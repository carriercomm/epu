#!/usr/bin/env python

"""
@file ion/services/dm/scheduler/test/receiver.py
@date 10/6/10
@author Paul Hubbard
@brief Simple listener to receive scheduler messages and report count
"""

from twisted.internet import defer
from ion.core.process.process import ProcessFactory
from ion.core.process.process import Process
import ion.util.ionlog
log = ion.util.ionlog.getLogger(__name__)

class ScheduledTask(Process):
    """
    Test listener class to receive scheduler messages.
    """
    def plc_init(self):
        self.msg_count = 0

    def op_scheduler(self, content, headers, msg):
        log.info('Got a scheduler message!')
        self.msg_count = self.msg_count + 1

    def op_get_count(self, content, headers, msg):
        self.reply_ok(msg, self.msg_count)


# Spawn of the process using the module name
factory = ProcessFactory(ScheduledTask)
