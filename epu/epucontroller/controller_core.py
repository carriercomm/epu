import ion.util.ionlog
log = ion.util.ionlog.getLogger(__name__)

import time
import uuid
from collections import defaultdict
from epu.decisionengine import EngineLoader
import epu.states as InstanceStates
from epu import cei_events
from twisted.internet.task import LoopingCall
from twisted.internet import defer
from epu.epucontroller.health import HealthMonitor

from forengine import Control
from forengine import State
from forengine import StateItem

PROVISIONER_VARS_KEY = 'provisioner_vars'

class ControllerCore(object):
    """Controller functionality that is not specific to the messaging layer.
    """

    def __init__(self, provisioner_client, engineclass, conf=None):
        self.state = ControllerCoreState()
        prov_vars = None
        if conf:
            if conf.has_key(PROVISIONER_VARS_KEY):
                prov_vars = conf[PROVISIONER_VARS_KEY]
                
        # There can only ever be one 'reconfigure' or 'decide' engine call run
        # at ANY time.  The 'decide' call is triggered via timed looping call
        # and 'reconfigure' is triggered asynchronously at any moment.  
        self.busy = defer.DeferredSemaphore(1)
        
        self.control = ControllerCoreControl(provisioner_client, self.state, prov_vars)
        self.engine = EngineLoader().load(engineclass)
        self.engine.initialize(self.control, self.state, conf)

    def new_sensor_info(self, content):
        """Ingests new sensor information, decides on validity and type of msg.
        """

        # Keeping message differentiation first, before state_item is parsed.
        # There needs to always be a methodical way to differentiate.
        if content.has_key("node_id"):
            self.state.new_instancestate(content)
        elif content.has_key("queue_id"):
            self.state.new_queuelen(content)
        else:
            log.error("received unknown sensor info: '%s'" % content)

    def new_heartbeat(self, content):
        """Ingests new heartbeat information
        """

        self.state.new_heartbeat(content)

    def begin_controlling(self):
        """Call the decision engine at the appropriate times.
        """
        log.debug('Starting engine decision loop - %s second interval',
                self.control.sleep_seconds)
        self.control_loop = LoopingCall(self.run_decide)
        self.control_loop.start(self.control.sleep_seconds, now=False)
        
    @defer.inlineCallbacks
    def run_decide(self):
        yield self.busy.run(self.engine.decide, self.control, self.state)
        
    @defer.inlineCallbacks
    def run_reconfigure(self, conf):
        yield self.busy.run(self.engine.reconfigure, self.control, conf)

        
class ControllerCoreState(State):
    """Keeps data, also what is passed to decision engine.

    In the future the decision engine will be passed more of a "view"
    """

    def __init__(self):
        super(ControllerCoreState, self).__init__()
        self.instance_state_parser = InstanceStateParser()
        self.queuelen_parser = QueueLengthParser()
        self.instance_states = defaultdict(list)
        self.queue_lengths = defaultdict(list)

        # TODO get monitor parameters from somewhere
        self.health = HealthMonitor()

    def new_instancestate(self, content):
        state_item = self.instance_state_parser.state_item(content)
        if state_item:
            self.instance_states[state_item.key].append(state_item)

            # need to send node state information to health monitor too.
            # it uses it to determine when nodes are missing or zombies
            self.health.node_state(state_item.key, state_item.value,
                                   state_item.time)

    def new_launch(self, new_instance_id):
        state = InstanceStates.REQUESTING
        item = StateItem("instance-state", new_instance_id, time.time(), state)
        self.instance_states[item.key].append(item)

    def new_queuelen(self, content):
        state_item = self.queuelen_parser.state_item(content)
        if state_item:
            self.queue_lengths[state_item.key].append(state_item)

    def new_heartbeat(self, content):
        self.health.new_heartbeat(content)

    def get_all(self, typename):
        """
        Get all data about a particular type.

        State API method, see the decision engine implementer's guide.

        @retval list(StateItem) StateItem instances that match the type
        or an empty list if nothing matches.
        @exception KeyError if typename is unknown
        """
        if typename == "instance-state":
            data = self.instance_states
        elif typename == "queue-length":
            data = self.queue_lengths
        elif typename == "instance-health":
            data = self.health.nodes.values()
        else:
            raise KeyError("Unknown typename: '%s'" % typename)

        return data.values()

    def get(self, typename, key):
        """Get all data about a particular key of a particular type.

        State API method, see the decision engine implementer's guide.

        @retval list(StateItem) StateItem instances that match the key query
        or an empty list if nothing matches.
        @exception KeyError if typename is unknown
        """
        if typename == "instance-state":
            data = self.instance_states
        elif typename == "queue-length":
            data = self.queue_lengths
        elif typename == "instance-health":
            data = self.health.nodes
        else:
            raise KeyError("Unknown typename: '%s'" % typename)

        if data.has_key(key):
            return data[key]
        else:
            return []


class InstanceStateParser(object):
    """Converts instance state message into a StateItem
    """

    def __init__(self):
        pass

    def state_item(self, content):
        log.debug("received new instance state message: '%s'" % content)
        try:
            instance_id = self._expected(content, "node_id")
            state = self._expected(content, "state")
        except KeyError:
            log.error("could not capture sensor info (full message: '%s')" % content)
            return None
        return StateItem("instance-state", instance_id, time.time(), state)

    def _expected(self, content, key):
        if content.has_key(key):
            return str(content[key])
        else:
            log.error("message does not contain part with key '%s'" % key)
            raise KeyError()

class QueueLengthParser(object):
    """Converts queuelen message into a StateItem
    """

    def __init__(self):
        pass

    def state_item(self, content):
        log.debug("received new queulen state message: '%s'" % content)
        try:
            queuelen = self._expected(content, "queuelen")
            queuelen = int(queuelen)
            queueid = self._expected(content, "queue_id")
        except KeyError:
            log.error("could not capture sensor info (full message: '%s')" % content)
            return None
        except ValueError:
            log.error("could not convert queulen into integer (full message: '%s')" % content)
            return None
        return StateItem("queue-length", queueid, time.time(), queuelen)

    def _expected(self, content, key):
        if content.has_key(key):
            return str(content[key])
        else:
            log.error("message does not contain part with key '%s'" % key)
            raise KeyError()


class ControllerCoreControl(Control):

    def __init__(self, provisioner_client, state, prov_vars):
        super(ControllerCoreControl, self).__init__()
        self.sleep_seconds = 5.0
        self.provisioner = provisioner_client
        self.state = state
        self.prov_vars = prov_vars # can be None

    def configure(self, parameters):
        """
        Give the engine the opportunity to offer input about how often it
        should be called or what specific events it would always like to be
        triggered after.

        See the decision engine implementer's guide for specific configuration
        options.

        @retval None
        @exception Exception illegal/unrecognized input
        """
        if not parameters:
            log.info("ControllerCoreControl is configured, no parameters")
            return
            
        if parameters.has_key("timed-pulse-irregular"):
            sleep_ms = int(parameters["timed-pulse-irregular"])
            self.sleep_seconds = sleep_ms / 1000.0
            log.info("Configured to pulse every %.2f seconds" % self.sleep_seconds)
            
        if parameters.has_key(PROVISIONER_VARS_KEY):
            self.prov_vars = conf[PROVISIONER_VARS_KEY]
            log.info("Configured with new provisioner vars:\n%s" % self.prov_vars)

    def launch(self, deployable_type_id, launch_description, extravars=None):
        """Choose instance IDs for each instance desired, a launch ID and send
        appropriate message to Provisioner.

        Control API method, see the decision engine implementer's guide.

        @param deployable_type_id string identifier of the DP to launch
        @param launch_description See engine implementer's guide
        @param extravars Optional, see engine implementer's guide
        @retval tuple (launch_id, launch_description), see guide
        @exception Exception illegal input
        @exception Exception message not sent
        """

        launch_id = str(uuid.uuid4())
        log.info("Request for DP '%s' is a new launch with id '%s'" % (deployable_type_id, launch_id))
        new_instance_id_list = []
        for group,item in launch_description.iteritems():
            log.info(" - %s is %d %s from %s" % (group, item.num_instances, item.allocation_id, item.site))
            for i in range(item.num_instances):
                new_instance_id = str(uuid.uuid4())
                self.state.new_launch(new_instance_id)
                item.instance_ids.append(new_instance_id)
                new_instance_id_list.append(new_instance_id)
        
        if extravars:
            vars_send = self.prov_vars.copy()
            vars_send.update(extravars)
        else:
            vars_send = self.prov_vars
            
        log.debug("Launching with parameters:\n%s" % str(vars_send))
            
        self.provisioner.provision(launch_id, deployable_type_id,
                launch_description, vars=vars_send)
        extradict = {"launch_id":launch_id,
                     "new_instance_ids":new_instance_id_list}
        cei_events.event("controller", "new_launch",
                         log, extra=extradict)
        return (launch_id, launch_description)

    def destroy_instances(self, instance_list):
        """Terminate particular instances.

        Control API method, see the decision engine implementer's guide.

        @param instance_list list size >0 of instance IDs to terminate
        @retval None
        @exception Exception illegal input/unknown ID(s)
        @exception Exception message not sent
        """
        self.provisioner.terminate_nodes(instance_list)

    def destroy_launch(self, launch_id):
        """Terminate an entire launch.

        Control API method, see the decision engine implementer's guide.

        @param launch_id launch to terminate
        @retval None
        @exception Exception illegal input/unknown ID
        @exception Exception message not sent
        """
        self.provisioner.terminate_launches([launch_id])
