import ion.util.ionlog
log = ion.util.ionlog.getLogger(__name__)

import os
import random
import signal
import sys
import time
import uuid
from collections import defaultdict

from epu.decisionengine import EngineLoader
from epu.epucontroller import Control
from epu.epucontroller import State
from epu.epucontroller import StateItem
import epu.states as InstanceStates
from epu.epucontroller import PROVISIONER_VARS_KEY

# -------
# HARNESS
# -------

class DecisionEngineExerciser(object):
    """
    This is a standalone controller which provides a 'mock' environment for
    running a decision engine.

    """

    def __init__(self, engineclass):
        self.continue_running = True
        self.engine = EngineLoader().load(engineclass)
        self.state = DeeState()
        self.control = DeeControl(self.state)

    def run_forever(self):
        """Initialize the decision engine and call 'decide' until killed."""

        conf = {'queuelen_high_water':'50', 'queuelen_low_water':'10'}
        self.engine.initialize(self.control, self.state, conf)
        while self.continue_running:
            time.sleep(self.control.sleep_seconds)
            self.update()
            self.engine.decide(self.control, self.state)
        log.warn("Controller is exiting")

    def crashing(self):
        """Experiment with crash scenarios (should the engine API change?)"""
        self.continue_running = False

    def update(self):
        all_qlens = self.state.get_all("queue-length")
        if not all_qlens:
            self.state.new_qlen(45)
        else:
            if len(all_qlens) != 1:
                raise Exception("only one queue at a time can be handled")
            qlens = all_qlens[0]
            latest = qlens[0]
            next = latest.value + random.randint(-40,40)
            if next < 0:
                next = 0
            self.state.new_qlen(next)

# ----------------------
# CONTROLLER API OBJECTS
# ----------------------

class DeeControl(Control):
    def __init__(self, deestate):
        super(DeeControl, self).__init__()
        self.sleep_seconds = 5.0
        self.deestate = deestate
        self.prov_vars = None
        # mini "mock" framework
        self.num_launched = 0
        self.total_launched = 0
        self.total_killed = 0

    def configure(self, parameters):
        """Control API method"""
        if not parameters:
            log.info("Control is configured, no parameters")
            return
            
        if parameters.has_key("timed-pulse-irregular"):
            sleep_ms = int(parameters["timed-pulse-irregular"])
            self.sleep_seconds = sleep_ms / 1000.0
            log.info("Configured to pulse every %.2f seconds" % self.sleep_seconds)
            
        if parameters.has_key(PROVISIONER_VARS_KEY):
            self.prov_vars = parameters[PROVISIONER_VARS_KEY]
            log.info("Configured with new provisioner vars:\n%s" % self.prov_vars)


    def launch(self, deployable_type_id, launch_description, extravars=None):
        """Control API method"""
        launch_id = uuid.uuid4()
        log.info("Request for DP '%s' is a new launch with id '%s'" % (deployable_type_id, launch_id))
        if extravars:
            log.info("Extra vars: %s" % extravars)
        for group,item in launch_description.iteritems():
            log.info(" - %s is %d %s from %s" % (group, item.num_instances, item.allocation_id, item.site))
            for i in range(item.num_instances):
                instanceid = uuid.uuid4()
                item.instance_ids.append(instanceid)
                self.deestate.new_launch(instanceid)
        self.num_launched += 1
        self.total_launched += 1
        return launch_id, launch_description

    def destroy_instances(self, instance_list):
        """Control API method"""
        for instanceid in instance_list:
            self.deestate.new_kill(instanceid)
            self.num_launched -= 1
            self.total_killed += 1

    def destroy_launch(self, launch_id):
        """Control API method"""
        raise NotImplementedError


class DeeState(State):
    def __init__(self, health=True):
        super(DeeState, self).__init__()
        self.instance_states = defaultdict(list)
        self.queue_lengths = defaultdict(list)
        if health:
            self.instance_health = {}
        else:
            self.instance_health = None

    def new_launch(self, new_instance_id):
        state = InstanceStates.RUNNING # magical instant-start
        item = StateItem("instance-state", new_instance_id, time.time(), state)
        self.instance_states[item.key].append(item)

    def new_kill(self, instanceid):
        state = InstanceStates.TERMINATING
        item = StateItem("instance-state", instanceid, time.time(), state)
        self.instance_states[item.key].append(item)

    def new_qlen(self, qlen):
        qlen_item = StateItem("queue-length", "x", time.time(), qlen)
        self.queue_lengths[qlen_item.key].append(qlen_item)

    def new_health(self, instance_id, is_ok=True):
        self.instance_health[instance_id] = DeeNodeHealth(instance_id, is_ok)

    def get_all(self, typename):
        if typename == "instance-state":
            data = self.instance_states
        elif typename == "queue-length":
            data = self.queue_lengths
        elif typename == "instance-health":
            data = self.instance_health
        else:
            raise KeyError("Unknown typename: '%s'" % typename)

        return data.values() if data is not None else None

    def get(self, typename, key):
        if typename == "instance-state":
            data = self.instance_states
        elif typename == "queue-length":
            data = self.queue_lengths
        elif typename == "instance-health":
            data = self.instance_health
        else:
            raise KeyError("Unknown typename: '%s'" % typename)

        if data and data.has_key(key):
            return data[key]
        else:
            return []

class DeeNodeHealth(object):
    def __init__(self, node_id, is_ok=True):
        self.node_id = node_id
        self.ok = is_ok
    def is_ok(self):
        return self.ok
    def __str__(self):
        return self.node_id

# ---------------
# SIGNAL HANDLING
# ---------------

def getcontroller():
    try:
        _controller
    except:
        return None
    return _controller

def setcontroller(controller):
    global _controller
    _controller = controller

def sigint_handler(signum, frame):
    log.critical("The sky is falling.")
    try:
        controller = getcontroller()
        if controller:
            controller.crashing()
    except:
        exception_type = sys.exc_type
        try:
            exceptname = exception_type.__name__
        except AttributeError:
            exceptname = exception_type
        err = "Problem: %s: %s" % (str(exceptname), str(sys.exc_value))
        log.error(err)
    os._exit(2)

# ----
# MAIN
# ----

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print >>sys.stderr, "ERROR, expecting argument: 'package.package.class' of decision engine to run."
        sys.exit(1)
    signal.signal(signal.SIGINT, sigint_handler)
    dee = DecisionEngineExerciser(sys.argv[1])
    setcontroller(dee)
    dee.run_forever()
