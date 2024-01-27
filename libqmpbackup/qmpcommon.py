#!/usr/bin/env python3
"""
 Copyright (C) 2022  Michael Ablassmeier

 Authors:
  Michael Ablassmeier <abi@grinser.de>

 This work is licensed under the terms of the GNU GPL, version 3.  See
 the LICENSE file in the top-level directory.
"""
import os
import logging
from time import sleep, time
from qemu.qmp import EventListener

from libqmpbackup import lib


class QmpCommon:
    """Common functions"""

    def __init__(self, qmp):
        self.qmp = qmp
        self.log = logging.getLogger(__name__)

    @staticmethod
    def transaction_action(action, **kwargs):
        """Return transaction action object"""
        return {
            "type": action,
            "data": dict((k.replace("_", "-"), v) for k, v in kwargs.items()),
        }

    def transaction_bitmap_clear(self, node, name, **kwargs):
        """Return transaction action object for bitmap clear"""
        return self.transaction_action(
            "block-dirty-bitmap-clear", node=node, name=name, **kwargs
        )

    def transaction_bitmap_add(self, node, name, **kwargs):
        """Return transaction action object for bitmap add"""
        return self.transaction_action(
            "block-dirty-bitmap-add", node=node, name=name, **kwargs
        )

    def prepare_transaction(self, argv, devices, backupdir):
        """Prepare transaction steps"""
        prefix = "FULL"
        sync = "full"
        if argv.level == "inc":
            prefix = "INC"
            sync = "incremental"

        bitmap_prefix = "qmpbackup"
        persistent = True
        if argv.level == "copy":
            prefix = "COPY"
            self.log.info("Copy backup: no persistent bitmap will be created.")
            bitmap_prefix = "qmpbackup-copy"
            persistent = False

        actions = []
        files = []
        for device in devices:
            timestamp = int(time())
            targetdir = f"{backupdir}/{device.node}/"
            os.makedirs(targetdir, exist_ok=True)
            filename = (
                f"{prefix}-{timestamp}-{os.path.basename(device.filename)}.partial"
            )
            target = f"{targetdir}/{filename}"
            files.append(target)
            bitmap = f"{bitmap_prefix}-{device.node}"
            job_id = f"{device.node}"

            if (
                not device.has_bitmap
                and argv.level in ("full", "copy")
                or device.has_bitmap
                and argv.level in ("copy")
            ):
                self.log.info("Creating new bitmap: %s", bitmap)
                actions.append(
                    self.transaction_bitmap_add(
                        device.node, bitmap, persistent=persistent
                    )
                )

            if device.has_bitmap and argv.level in ("full"):
                self.log.debug("Clearing existing bitmap")
                actions.append(self.transaction_bitmap_clear(device.node, bitmap))

            if argv.level in ("full", "copy"):
                actions.append(
                    self.transaction_action(
                        "drive-backup",
                        device=device.node,
                        target=target,
                        sync=sync,
                        job_id=job_id,
                        speed=argv.speed_limit,
                    )
                )
            else:
                actions.append(
                    self.transaction_action(
                        "drive-backup",
                        bitmap=bitmap,
                        device=device.node,
                        target=target,
                        sync=sync,
                        job_id=job_id,
                        speed=argv.speed_limit,
                    )
                )

        self.log.debug("Created transaction: %s", actions)

        return actions, files

    async def backup(self, argv, devices, backupdir, qga):
        """Start backup transaction, while backup is active,
        watch for block status"""
        actions, files = self.prepare_transaction(argv, devices, backupdir)
        listener = EventListener(
            (
                "BLOCK_JOB_COMPLETED",
                "BLOCK_JOB_CANCELLED",
                "BLOCK_JOB_ERROR",
                "BLOCK_JOB_READY",
                "BLOCK_JOB_PENDING",
                "JOB_STATUS_CHANGE",
            )
        )
        with self.qmp.listen(listener):
            await self.qmp.execute("transaction", arguments={"actions": actions})
            if qga is not False:
                lib.thaw(qga)
            async for event in listener:
                if event["event"] == "BLOCK_JOB_COMPLETED":
                    self.log.info("Saved all disks")
                    break
                if event["event"] in ("BLOCK_JOB_ERROR", "BLOCK_JOB_CANCELLED"):
                    raise RuntimeError(
                        f"Error during backup operation: {event['event']}"
                    )

                while True:
                    jobs = await self.qmp.execute("query-block-jobs")
                    if not jobs:
                        break
                    self.progress(jobs, devices)
                    sleep(1)

        return files

    async def do_query_block(self):
        """Return list of attached block devices"""
        devices = await self.qmp.execute("query-block")
        return devices

    async def remove_bitmaps(self, blockdev, prefix="qmpbackup"):
        """Remove existing bitmaps for block devices"""
        for dev in blockdev:
            if not dev.has_bitmap:
                self.log.info("No bitmap set for device %s", dev.node)
                continue

            for bitmap in dev.bitmaps:
                bitmap_name = bitmap["name"]
                self.log.debug("Bitmap name: %s", bitmap_name)
                if prefix not in bitmap_name:
                    self.log.debug("Ignoring bitmap: %s", bitmap_name)
                    continue
                self.log.info("Removing bitmap: %s", f"{prefix}-{dev.node}")
                await self.qmp.execute(
                    "block-dirty-bitmap-remove",
                    arguments={"node": dev.node, "name": f"{prefix}-{dev.node}"},
                )

    def progress(self, jobs, devices):
        """Report progress for active block job"""
        for device in devices:
            for job in jobs:
                if job["device"] == device.node:
                    prog = [
                        round(job["offset"] / job["len"] * 100)
                        if job["offset"] != 0
                        else 0
                    ]
                    self.log.info(
                        "[%s:%s] Wrote Offset: %s%% (%s of %s)",
                        job["device"],
                        os.path.basename(device.filename),
                        prog[0],
                        job["offset"],
                        job["len"],
                    )
