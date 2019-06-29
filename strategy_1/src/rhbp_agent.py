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
        self.number_of_agents = 1 # TODO: check if there's a way to get it automatically

        self._sim_started = False

        # agent attributes
        self.local_map = GridMap(agent_name=self._agent_name, agent_vision=5)  # TODO change to get the vision
        self.map_messages_buffer = []

        # representation of tasks
        self.tasks = {}
        self.assigned_tasks = [] # personal for the agent

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

        self._received_action_response = False

    
    def calculateSubTaskBid(self, subtask):
        bid_value = -1

        if self.local_map.goal_area_fully_discovered:
            required_type = subtask.type

            # find the closest dispenser
            min_dist = 9999
            pos = [[-1,-1]]

            for dispenser in self.local_map._dispensers:
                if dispenser.type == required_type: # check if the type is the one we need
                    pos_matrix = self.local_map._from_relative_to_matrix(dispenser.pos)
                    dist = self.local_map._distances[pos_matrix[0],pos_matrix[1]]

                    if dist < min_dist and dist != -1: # see if the distance is minimum and save it
                        min_dist = dist
                        pos[0] = pos_matrix
            

            if min_dist != 9999: # the distance to the closer dispenser has been calculated
                # add the distance to the goal
                landmark = self.local_map._from_relative_to_matrix(self.local_map.goal_top_left)
                end = [[landmark[0], landmark[1]]]
                path = self.local_map.path_planner.astar(
                    maze=self.local_map._path_planner_representation,
                    origin=self.local_map.origin,
                    start=np.array(pos, dtype=np.int),
                    end=np.array(end, dtype=np.int))

                if path is not None:
                    bid_value = len(path) + min_dist # distance from agent to dispenser + dispenser to goal
                    print(self._agent_name + ": " + str(bid_value))
                else:
                    print(self._agent_name + ": No bid")

        return bid_value


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

        ### breakpoint after 30 steps to debug task subdivision every 30 steps
        if self.perception_provider.simulation_step % 30 == 0 and self.perception_provider.simulation_step > 0:
            rospy.logdebug('Simulationstep {}'.format(self.perception_provider.simulation_step))

        self._received_action_response = False

        # ###### UPDATE AND SYNCHRONIZATION ######
        #
        # update map
        self.local_map.update_map(agent=msg.agent, perception=self.perception_provider)
        # # best_point, best_path, current_high_score = self.local_map.get_point_to_explore()
        # # rospy.logdebug("Best point: " + str(best_point))
        # # rospy.logdebug("Best path: " + str(best_path))
        # # rospy.logdebug("Current high score: " + str(current_high_score))

        """
        # update tasks
        self.tasks = update_tasks(current_tasks=self.tasks, tasks_percept=self.perception_provider.tasks,
                                  simulation_step=self.perception_provider.simulation_step)
        rospy.loginfo("{} updated tasks. New amount of tasks: {}".format(self._agent_name, len(self.tasks)))

        for task_name, task_object in self.tasks.iteritems():
            # TODO: possible optimization to free memory -> while we cycle all the tasks, check for if complete and if yes remove from the task list?

            rospy.logdebug("-- Analyizing: " + task_name)
            assigned = []

            for sub in task_object.sub_tasks:
                if (sub.assigned_agent == None):
                    subtask_id = sub.sub_task_name
                    rospy.logdebug("---- Bid needed for " + subtask_id)
                    
                    # check if the agent is already assigned to some subtasks of the same parent 
                    if (self._agent_name in assigned):
                        bid_value = 9999
                    else:
                        # first calculate the already assigned sub tasks
                        bid_value = 0
                        for t in self.assigned_tasks:
                            bid_value +=  self.calculateSubTaskBid(t)

                        # add the current

                        bid_value +=  self.calculateSubTaskBid(sub)

                    self._communication.send_bid(self._pub_auction, subtask_id, bid_value)

                    # wait until the bid is done
                    while subtask_id not in self.bids:
                        pass

                    while self.bids[subtask_id]["done"] == None:
                        pass

                    if self.bids[subtask_id]["done"] != "-1": # was a valid one
                        rospy.logdebug("------ DONE: " + str(self.bids[subtask_id]["done"]) + " with bid value: " + str(bid_value))
                        sub.assigned_agent = self.bids[subtask_id]["done"]

                        assigned.append(sub.assigned_agent)

                        if sub.assigned_agent == self._agent_name:
                            self.assigned_tasks.append(sub)
                    else:
                        rospy.logdebug("------ INVALID: " + str(self.bids[subtask_id]["done"]) + " with bid value: " + str(bid_value))

                    del self.bids[sub.sub_task_name]  # free memory
            
            if not task_object.auctioned: # if not all the subtasks were fully auctioned, reset all the subtasks 
                if len(assigned) < len(task_object.sub_tasks):
                    rospy.logdebug("--------- NEED TO REMOVE: " + str(task_object.auctioned))
                    for sub in task_object.sub_tasks:
                        sub.agent_assigned = None
                        if sub in self.assigned_tasks:
                            self.assigned_tasks.remove(sub)

        ########################################
        """

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
                # map received
                maps = np.fromstring(map_value, dtype=int).reshape(map_rows, map_columns)
                rospy.logdebug(maps)
                map_received = np.copy(maps)
                # landmark received
                lm_received = (map_lm_y, map_lm_x)
                # own landmark
                lm_own = self.local_map._from_relative_to_matrix(self.local_map.goal_top_left)
                # do map merge
                # Added new origin to function
                origin_own = np.copy(self.local_map.origin)
                merged_map, merged_origin = mapMerge(map_received, self.local_map._representation, lm_received, lm_own, origin_own)
                self.local_map._representation = np.copy(merged_map)
                self.local_map.origin = np.copy(merged_origin)

            self.map_messages_buffer.remove(msg)

        # send the map if perceive the goal
        if self.local_map.goal_area_fully_discovered:
            map = self.local_map._representation
            top_left_corner = self.local_map._from_relative_to_matrix(self.local_map.goal_top_left)
            self._communication.send_map(self._pub_map, map.tostring(), top_left_corner[0], top_left_corner[1],
                                         map.shape[0], map.shape[1])  # lm_x and lm_y to get

        '''
        # send personal message test
        if self._agent_name == "agentA1":
            self._communication.send_message(self._pub_agents, "agentA2", "task", "[5,5]")

        self._received_action_response = False
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

        if task_id not in self.bids:
            self.bids[task_id] = OrderedDict()
            self.bids[task_id]["done"] = None

        if self.bids[task_id]["done"] is None:
            if msg_from not in self.bids[task_id]:
                self.bids[task_id][msg_from] = task_bid_value

            if len(self.bids[task_id]) == self.number_of_agents + 1:  # count the done
                ordered_task = OrderedDict(sorted(self.bids[task_id].items(), key=lambda x: (x[1], x[0])))

                '''

                This in case we want to extend it to the possibility of more than one agent assigned to a sub task
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
                '''

                i = 0
                for key, value in ordered_task.items():
                    if (i > 0):  # skip done
                        if (value == -1):
                            self.bids[task_id]["done"] = "-1"
                        else:
                            self.bids[task_id]["done"] = key
                            break

                    i += 1

    def _initialize_behaviour_model(self):
        """
        This function initialises the RHBP behaviour/goal model.
        """

        # Exploration
        exploration_move = ExplorationBehaviour(name="exploration_move", agent_name=self._agent_name, rhbp_agent=self)
        self.behaviours.append(exploration_move)
        # exploration_move.add_effect(Effect(self.perception_provider.dispenser_visible_sensor.name, indicator=True))

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
