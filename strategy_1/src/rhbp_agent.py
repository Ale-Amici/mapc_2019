#!/usr/bin/env python2

import rospy
import os
from mapc_ros_bridge.msg import RequestAction, GenericAction, SimStart, SimEnd, Bye

from behaviour_components.managers import Manager
from behaviour_components.condition_elements import Effect


from agent_commons.behaviour_classes.exploration_behaviour import ExplorationBehaviour
from agent_commons.providers import PerceptionProvider
from agent_commons.agent_utils import get_bridge_topic_prefix


import global_variables

from classes.grid_map import GridMap
from classes.tasks.task_decomposition import update_tasks

from classes.communications import Communication

from classes.map_merge import mapMerge

from collections import OrderedDict

import random
import numpy as np

class RhbpAgent(object):
    """
    Main class of an agent, taking care of the main interaction with the mapc_ros_bridge
    """

    def __init__(self):
        ###DEBUG MODE###

        log_level = rospy.DEBUG if global_variables.DEBUG_MODE else rospy.INFO
        ################
        rospy.logdebug("RhbpAgent::init")

        rospy.init_node('agent_node', anonymous=True, log_level=log_level)

        self._agent_name = rospy.get_param('~agent_name', 'agentA1')  # default for debugging 'agentA1'

        self._agent_topic_prefix = get_bridge_topic_prefix(agent_name=self._agent_name)

        # ensure also max_parallel_behaviours during debugging
        self._manager = Manager(prefix=self._agent_name, max_parallel_behaviours=1)

        self.behaviours = []
        self.goals = []

        self.perception_provider = PerceptionProvider()

        # auction structure

        self.bids = {}
        self.number_of_agents = 2  # TODO: check if there's a way to get it automatically

        self._sim_started = False

        # agent attributes
        self.local_map = GridMap(agent_name=self._agent_name, agent_vision=5) # TODO change to get the vision
        self.map_messages_buffer = []

        # representation of tasks
        self.tasks = {}

        # subscribe to MAPC bridge core simulation topics
        rospy.Subscriber(self._agent_topic_prefix + "request_action", RequestAction, self._action_request_callback)

        rospy.Subscriber(self._agent_topic_prefix + "start", SimStart, self._sim_start_callback)

        rospy.Subscriber(self._agent_topic_prefix + "end", SimEnd, self._sim_end_callback)

        rospy.Subscriber(self._agent_topic_prefix + "bye", Bye, self._bye_callback)

        rospy.Subscriber(self._agent_topic_prefix + "generic_action", GenericAction, self._callback_generic_action)

        # start communication class
        self._communication = Communication(self._agent_name)
        # Map topic
        self._pub_map = self._communication.start_map(self._callback_map)
        # Personal message topic
        self._pub_agents = self._communication.start_agents(self._callback_agents)
        # Auction topic
        self._pub_auction = self._communication.start_auction(self._callback_auction)
        self.time_to_bid = True  # only test debug puposes
        self.task_subdivision = {"task1":
                                     {"agents_needed": 3,
                                      "agents_assigned": []
                                      }
                                 }

        self._received_action_response = False

    def _sim_start_callback(self, msg):
        """
        here we could also evaluate the msg in order to initialize depending on the role etc.
        :param msg:  the message
        :type msg: SimStart
        """

        if not self._sim_started:  # init only once here

            rospy.loginfo(self._agent_name + " started")

            # creating the actual RHBP model
            self._initialize_behaviour_model()

        self._sim_started = True

    def _callback_generic_action(self, msg):
        """
        ROS callback for generic actions
        :param msg: ros message
        :type msg: GenericAction
        """
        self._received_action_response = True

    def _sim_end_callback(self, msg):
        """
        :param msg:  the message
        :type msg: SimEnd
        """
        rospy.loginfo("SimEnd:" + str(msg))
        for g in self.goals:
            g.unregister()
        for b in self.behaviours:
            b.unregister()
        self._sim_started = False

    def _bye_callback(self, msg):
        """
        :param msg:  the message
        :type msg: Bye
        """
        rospy.loginfo("Simulation finished")
        rospy.signal_shutdown('Shutting down {}  - Simulation server closed'.format(self._agent_name))

    def _action_request_callback(self, msg):
        """
        here we just trigger the decision-making and planning
        while tracking the available time and behaviour responses
        :param msg: the message
        :type msg: RequestAction
        """

        # calculate deadline for the current simulation step
        start_time = rospy.get_rostime()
        safety_offset = rospy.Duration.from_sec(0.2)  # Safety offset in seconds
        deadline_msg = rospy.Time.from_sec(msg.deadline / 1000.0)
        current_msg = rospy.Time.from_sec(msg.time / 1000.0)
        deadline = start_time + (deadline_msg - current_msg) - safety_offset

        self.perception_provider.update_perception(request_action_msg=msg)

        self._received_action_response = False

        ###### UPDATE AND SYNCHRONIZATION ######

        # update map
        self.local_map.update_map(agent=msg.agent, perception=self.perception_provider)
        #best_point, best_path, current_high_score = self.local_map.get_point_to_explore()
        #rospy.logdebug("Best point: " + str(best_point))
        #rospy.logdebug("Best path: " + str(best_path))
        #rospy.logdebug("Current high score: " + str(current_high_score))

        # update tasks
        self.tasks = update_tasks(current_tasks=self.tasks, tasks_percept=self.perception_provider.tasks, simulation_step=self.perception_provider.simulation_step)
        rospy.logdebug("{} updated tasks. New amount of tasks: {}".format(self._agent_name, len(self.tasks)))
        ########################################

        # process the maps in the buffer

        for msg in self.map_messages_buffer[:]:
            msg_id = msg.message_id
            map_from = msg.agent_id
            map_value = msg.map
            map_lm_x = msg.lm_x
            map_lm_y = msg.lm_y
            map_rows = msg.rows
            map_columns = msg.columns

            if map_from != self._agent_name and self.local_map.goal_area_fully_discovered:
                rospy.loginfo(self._agent_name + " received map from " + map_from + " | map value: " + map_value)
                '''
                map = np.fromstring(map_value, dtype=int).reshape(map_rows, map_columns)
                lm = [map_lm_x,map_lm_y]
                lm2 = self.local_map._from_relative_to_matrix(self.local_map.goal_top_left)
                # do map merge
                merged_map = mapMerge(map,self.local_map._representation,lm,lm2)

                self.local_map._representation = merged_map
                #rospy.logdebug('MAPPA MERGED')
                #rospy.logdebug(str(merged_map))
                '''
            
            self.map_messages_buffer.remove(msg)


        # send the map if perceive the goal
        if self.local_map.goal_area_fully_discovered:
            map = self.local_map._representation
            top_left_corner = self.local_map._from_relative_to_matrix(self.local_map.goal_top_left)
            self._communication.send_map(self._pub_map, map.tostring(), top_left_corner[0], top_left_corner[1], map.shape[0], map.shape[1])  # lm_x and lm_y to get

        '''
        # send personal message test
        if self._agent_name == "agentA1":
            self._communication.send_message(self._pub_agents, "agentA2", "task", "[5,5]")

        self._received_action_response = False

        # send bid 
        if self.time_to_bid:
            task_to_bid = "task1"
            if (self._agent_name == "agentA1" or self._agent_name == "agentA2"):
                self._communication.send_bid(self._pub_auction, task_to_bid, 10)
            else:
                self._communication.send_bid(self._pub_auction, task_to_bid, random.randint(1, 100))
            self.time_to_bid = False
            rospy.loginfo(self._agent_name + " ha biddato")
        '''

        

        # self._received_action_response is set to True if a generic action response was received(send by any behaviour)
        while not self._received_action_response and rospy.get_rostime() < deadline:
            # wait until this agent is completely initialised
            if self._sim_started:  # we at least wait our max time to get our agent initialised

                # action send is finally triggered by a selected behaviour
                self._manager.step(guarantee_decision=True)
            else:
                rospy.sleep(0.1)

        if self._received_action_response:  # One behaviour replied with a decision
            duration = rospy.get_rostime() - start_time
            rospy.logdebug("%s: Decision-making duration %f", self._agent_name, duration.to_sec())

        elif not self._sim_started:  # Agent was not initialised in time
            rospy.logwarn("%s idle_action(): sim not yet started", self._agent_name)
        else:  # Our decision-making has taken too long
            rospy.logwarn("%s: Decision-making timeout", self._agent_name)

    def _callback_map(self, msg):
        self.map_messages_buffer.append(msg)            

    def _callback_agents(self, msg):
        msg_id = msg.message_id
        msg_from = msg.agent_id_from
        msg_type = msg.message_type
        msg_param = msg.params

        if msg.agent_id_to == self._agent_name:
            rospy.loginfo(
                self._agent_name + " received message from " + msg_from + " | id: " + msg_id + " | type: " + msg_type + " | params: " + msg_param)
            self._communication.send_message(self._pub_agents, msg_from, "received", msg_id)

    def _callback_auction(self, msg):
        msg_id = msg.message_id
        msg_from = msg.agent_id
        task_id = msg.task_id
        task_bid_value = msg.bid_value

        if (not task_id in self.bids):
            self.bids[task_id] = OrderedDict()
            self.bids[task_id]["done"] = False

        if (self.bids[task_id]["done"] == False):
            if (not msg_from in self.bids[task_id]):
                self.bids[task_id][msg_from] = task_bid_value

            if len(self.bids[task_id]) == self.number_of_agents + 1:  # count the done
                ordered_task = OrderedDict(sorted(self.bids[task_id].items(), key=lambda x: (x[1], x[0])))
                # rospy.loginfo(self._agent_name +  " | " + str(ordered_task))

                duplicate = -999
                i = 0
                for key, value in ordered_task.items():
                    if (i > 0):  # skip done
                        if (i == self.task_subdivision[task_id]["agents_needed"] + 1):
                            break

                        available = (len(ordered_task) - 1) - len(self.task_subdivision[task_id]["agents_assigned"]) - i
                        # rospy.loginfo(self._agent_name + " |1: " + str(len(ordered_task) - 1) + " | 2: " + str(len(self.task_subdivision[task_id]["agents_assigned"])) + "i: " + str(i) + " | current:" + key)
                        if (value != duplicate or available <= 0):
                            self.task_subdivision[task_id]["agents_assigned"].append(key)

                        duplicate = value

                    i += 1

                self.bids[task_id]["done"] = True
                rospy.loginfo("DONE: " + str(self.task_subdivision[task_id]["agents_assigned"]))

    def _initialize_behaviour_model(self):
        """
        This function initialises the RHBP behaviour/goal model.
        """

        # Exploration
        exploration_move = ExplorationBehaviour(name="exploration_move", agent_name=self._agent_name, rhbp_agent=self)
        self.behaviours.append(exploration_move)
        #exploration_move.add_effect(Effect(self.perception_provider.dispenser_visible_sensor.name, indicator=True))

        """
        # Random Move/Exploration
        random_move = RandomMove(name="random_move", agent_name=self._agent_name)
        self.behaviours.append(random_move)
        random_move.add_effect(Effect(self.perception_provider.dispenser_visible_sensor.name, indicator=True))

        
        # Moving to a dispenser if in vision range
        move_to_dispenser = MoveToDispenser(name="move_to_dispense", perception_provider=self.perception_provider,
                                            agent_name=self._agent_name)
        self.behaviours.append(move_to_dispenser)
        move_to_dispenser.add_effect(
            Effect(self.perception_provider.closest_dispenser_distance_sensor.name, indicator=-1, sensor_type=float))
        move_to_dispenser.add_precondition(
            Condition(self.perception_provider.dispenser_visible_sensor, BooleanActivator(desiredValue=True)))
        move_to_dispenser.add_precondition(Condition(self.perception_provider.closest_dispenser_distance_sensor,
                                            ThresholdActivator(isMinimum=True, thresholdValue=2)))

        # Dispense a block if close enough
        dispense = Dispense(name="dispense", perception_provider=self.perception_provider, agent_name=self._agent_name)
        self.behaviours.append(dispense)
        dispense.add_effect(
            Effect(self.perception_provider.number_of_blocks_sensor.name, indicator=+1, sensor_type=float))

        dispense.add_precondition(Condition(self.perception_provider.closest_dispenser_distance_sensor,
                                            ThresholdActivator(isMinimum=False, thresholdValue=1)))

        # Our simple goal is to create more and more blocks
        dispense_goal = GoalBase("dispensing", permanent=True,
                                 conditions=[Condition(self.perception_provider.number_of_blocks_sensor, GreedyActivator())],
                                 planner_prefix=self._agent_name)
        self.goals.append(dispense_goal)
        """

if __name__ == '__main__':
    try:
        rhbp_agent = RhbpAgent()

        rospy.spin()

    except rospy.ROSInterruptException:
        rospy.logerr("program interrupted before completion")
