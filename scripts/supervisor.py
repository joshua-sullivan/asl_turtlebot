#!/usr/bin/env python

import rospy
from gazebo_msgs.msg import ModelStates
from std_msgs.msg import Float32MultiArray, String, Bool, Int8
from geometry_msgs.msg import Twist, PoseArray, Pose2D, PoseStamped, PoseWithCovarianceStamped
from asl_turtlebot.msg import DetectedObject, TSalesRequest, TSalesCircuit
import landmarks
import tf
import math
from sound_play.msg import SoundRequest
from sound_play.libsoundplay import SoundClient
# from asl_turtlebot import finalcount.wav
from enum import Enum
import numpy as np
import traveling_salesman
import pdb

# threshold at which we consider the robot at a location
POS_EPS = .3
THETA_EPS = .3

# time to stop at a stop sign
STOP_TIME = 3

# minimum distance from a stop sign to obey it
STOP_MIN_DIST = .3

# minimum distance frmo a stop sign to leave CROSS mode
CROSS_MIN_DIST = .4

# time taken to cross an intersection
CROSSING_TIME = 3

# time taken to rescue an animal
RESCUE_TIME = 3

# Distance threshold to consider to stop detections as
# the same stop sign
STOP_SIGN_DIST_THRESH = 0.6

# Distance threshold to consider to animal detections as
# the same stop sign
ANIMAL_DIST_THRESH = 0.6

# Duration for continuous time not seeing a bike before moving again
BIKE_STOP_TIME = 5

# Minimum number of observations of an animal to consider it valid
ANIMAL_MIN_OBSERVATIONS = 3

# Nominal number of observations of an animal before moving on from pinpointing
ANIMAL_NOM_OBSERVATIONS = 10

# Maximum amount of time to allow between first detection and moving on to previous goal pose during poinpointing
MAX_PINPOINT_TIME = 3.0

# state machine modes, not all implemented
class Mode(Enum):
    IDLE = 0
    STOP = 1
    CROSS = 2
    NAV = 3
    PLAN_RESCUE = 4
    REQUEST_RESCUE = 5
    GO_TO_ANIMAL = 6
    RESCUE_ANIMAL = 7
    EXPLORE = 8
    VICTORY = 9
    BIKE_STOP = 10
    GO_TO_EXPLORE_WAYPOINT = 11

class Supervisor:
    """ the state machine of the turtlebot """

    def __init__(self):
        rospy.init_node('turtlebot_supervisor', anonymous=True)

        self.soundhandle = SoundClient()

        # current pose
        self.x = 0
        self.y = 0
        self.theta = 0

        # pose goal
        self.x_g = 0
        self.y_g = 0
        self.theta_g = 0
        self.pose_goal_backlog = []

        # init flag used for running init function for each state
        self.init_flag = 0

        # Landmark lists
        self.stop_signs = landmarks.StopSigns(dist_thresh=STOP_SIGN_DIST_THRESH) # List of coordinates for all stop signs
        self.animal_waypoints = landmarks.AnimalWaypoints(dist_thresh=ANIMAL_DIST_THRESH)
        self.explore_waypoints = landmarks.ExploreWaypoints()

        # flag that determines if the rescue can be initiated
        self.rescue_on = False
        #flag to keep sound from repeating
        self.play_rescue_sound = True
        self.victory_sound = True

        # flag that determines if the robot has found a bicycle and should honk
        #self.bicycles = []
        self.honk = False
        self.playsound = True

        # string for target animal
        self.target_animal = None

        # status flag for traveling salesman circuit received
        self.tsales_circuit_received = 1

        # status flag for amcl init received
        self.amcl_init_received = False

        # status flag for whether exploration has started
        self.explore_started = False

        # lock waypoints
        self.lock_animal_waypoints = 0

        # status flag for whether navigating to animal while exploring
        self.nav_to_animal_CCW = False
        self.nav_to_animal_CW = False

        # time of starting to spin to animal
        self.time_of_spin = 0.0

        # status flag to check if current theta is smaller than goal theta
        self.prev_th_less_than_g = False

        # current mode
        self.mode = Mode.IDLE
        self.modeafterstop = Mode.IDLE
        self.modeafterhonk = Mode.IDLE
        self.last_mode_printed = None

        self.nav_goal_publisher = rospy.Publisher('/cmd_nav', Pose2D, queue_size=10)
        self.cmd_vel_publisher = rospy.Publisher('/cmd_vel', Twist, queue_size=10)
        self.rescue_ready_publisher = rospy.Publisher('/ready_to_rescue', Bool, queue_size=10)
        self.tsales_request_publisher = rospy.Publisher('/tsales_request', TSalesRequest, queue_size=10)

        rospy.Subscriber('/detector/stop_sign', DetectedObject, self.stop_sign_detected_callback)
        rospy.Subscriber('/move_base_simple/goal', PoseStamped, self.rviz_goal_callback)
        # rospy.Subscriber('/detector/bird', DetectedObject, self.animal_detected_callback)
        rospy.Subscriber('/detector/cat', DetectedObject, self.animal_detected_callback)
        rospy.Subscriber('/detector/dog', DetectedObject, self.animal_detected_callback)
        # rospy.Subscriber('/detector/horse', DetectedObject, self.animal_detected_callback)
        # rospy.Subscriber('/detector/sheep', DetectedObject, self.animal_detected_callback)
        # rospy.Subscriber('/detector/cow', DetectedObject, self.animal_detected_callback)
        rospy.Subscriber('/detector/elephant', DetectedObject, self.animal_detected_callback)
        # rospy.Subscriber('/detector/bear', DetectedObject, self.animal_detected_callback)
        # rospy.Subscriber('/detector/zebra', DetectedObject, self.animal_detected_callback)
        # rospy.Subscriber('/detector/giraffe', DetectedObject, self.animal_detected_callback)
        rospy.Subscriber('/detector/bicycle', DetectedObject, self.bicycle_detected_callback)
        rospy.Subscriber('/rescue_on', Bool, self.rescue_on_callback)
        rospy.Subscriber('/cmd_state', Int8, self.cmd_state_callback)
        rospy.Subscriber('/tsales_circuit', TSalesCircuit, self.tsales_circuit_callback)
        rospy.Subscriber('/initialpose', PoseWithCovarianceStamped, self.amcl_init_callback)

        self.trans_listener = tf.TransformListener()

    def cmd_state_callback(self, msg):
        self.mode = Mode(msg.data)

    def rviz_goal_callback(self, msg):
        """ callback for a pose goal sent through rviz """

        self.x_g = msg.pose.position.x
        self.y_g = msg.pose.position.y
        rotation = [msg.pose.orientation.x, msg.pose.orientation.y, msg.pose.orientation.z, msg.pose.orientation.w]
        euler = tf.transformations.euler_from_quaternion(rotation)
        self.theta_g = euler[2]

    def stop_sign_detected_callback(self, msg):
        """ callback for when the detector has found a stop sign. Note that
        a distance of 0 can mean that the lidar did not pickup the stop sign at all """

        # Check the location is valid i.e. 3rd element is non-zero
        if msg.location_W[2] == 1.0:
            observation = msg.location_W[:2]
            self.stop_signs.add_observation(observation)

    def stop_check(self):
        """ checks if within stopping threshold """
        current_pose = [self.x, self.y]
        dist2stop = []
        for i in range(self.stop_signs.locations.shape[0]):
            dist2stop.append(np.linalg.norm(current_pose - self.stop_signs.locations[i,:])) # Creates list of distances to all stop signs
        return any(dist < STOP_MIN_DIST for dist in dist2stop)

    def animal_detected_callback(self, msg):
        """ callback for when the detector has found an animal """

        # Check the location is valid i.e. 3rd element is non-zero
        if msg.location_W[2] == 1.0:
            pose = np.array([self.x, self.y, self.theta])
            bbox_height = msg.corners[3] - msg.corners[1]

            observation = msg.location_W[:2]

            animal_type = msg.name

            # only add animals in the exploration states
            if not self.lock_animal_waypoints:
                idx = self.animal_waypoints.add_observation(observation, pose, bbox_height, animal_type, msg.location_W[3])

                
                # Finds number of observations of this existing detection
                n = self.animal_waypoints.observations_count[idx] 
                # If first detection, stop bacon and pinpoint
                if n == 1:
                    # Stop Bacon in its tracks and pinpoint
                    # self.x_g = self.x
                    # self.y_g = self.y
                    
                    # Set the attitude to "center" detected box in image frame
                    # self.theta_g = self.animal_waypoints.animal_theta_g[idx]
                    # self.theta_g = self.theta

                    if self.animal_waypoints.animal_theta_g[idx] < self.theta:
                        self.nav_to_animal_CW = True

                    elif self.animal_waypoints.animal_theta_g[idx] > self.theta:
                        self.nav_to_animal_CCW = True

                    t_first = rospy.get_rostime().to_sec()
                    self.time_of_spin = t_first
                    self.animal_waypoints.first_detection(t_first)

                    # self.nav_to_animal = True

                elif self.close_to(self.x, self.y, self.animal_waypoints.animal_theta_g[idx]):
                    self.nav_to_animal_CW = False
                    self.nav_to_animal_CCW = False
                    self.x_g = self.pose_goal_backlog[0]
                    self.y_g = self.pose_goal_backlog[1]
                    self.theta_g = self.pose_goal_backlog[2]

                    # self.nav_to_animal = False
                    
                # Once the nominal number of animal measurements is achieved, reset to prior goal pose
                elif n > ANIMAL_NOM_OBSERVATIONS:
                    self.nav_to_animal_CW = False
                    self.nav_to_animal_CCW = False
                    self.x_g = self.pose_goal_backlog[0]
                    self.y_g = self.pose_goal_backlog[1]
                    self.theta_g = self.pose_goal_backlog[2]

                # In case the nominal number of measurements never occurs (likely from spurious objection detected initially),
                # wait a fixed amount of time before moving on to prior goal pose
                elif rospy.get_rostime().to_sec() > self.animal_waypoints.time_first_detection[idx] + MAX_PINPOINT_TIME:
                    self.nav_to_animal_CW = False
                    self.nav_to_animal_CCW = False
                    self.x_g = self.pose_goal_backlog[0]
                    self.y_g = self.pose_goal_backlog[1]
                    self.theta_g = self.pose_goal_backlog[2]

                    # self.nav_to_animal = False

                    print 'timed out on animal'


    def bicycle_detected_callback(self, msg):
    	"""callback for when the detector has found a bicycle"""
        self.honk = True
        self.bike_detected_start = rospy.get_rostime()

    def rescue_on_callback(self, msg):
        """callback for when the rescue is ready"""
        self.rescue_on = msg.data

    def nav_to_pose(self):
        """ sends the current desired pose to the navigator """
        nav_g_msg = Pose2D()
        nav_g_msg.x = self.x_g
        nav_g_msg.y = self.y_g
        nav_g_msg.theta = self.theta_g

        self.nav_goal_publisher.publish(nav_g_msg)

    def stay_idle(self):
        """ sends zero velocity to stay put """
        vel_g_msg = Twist()
        self.cmd_vel_publisher.publish(vel_g_msg)

    def turn_CCW(self):
        """turns the robot left when detecting animal"""
        twist = Twist()
        twist.linear.x = 0; twist.linear.y = 0; twist.linear.z = 0
        twist.angular.x = 0; twist.angular.y = 0; twist.angular.z = 0.1
        self.cmd_vel_publisher.publish(twist)

    def turn_CW(self):
        """turns the robot CW when detecting animal"""
        twist = Twist()
        twist.linear.x = 0; twist.linear.y = 0; twist.linear.z = 0
        twist.angular.x = 0; twist.angular.y = 0; twist.angular.z = -0.1
        self.cmd_vel_publisher.publish(twist)

    def close_to(self,x,y,theta):
        """ checks if the robot is at a pose within some threshold """
        # if (abs(x-self.x)<POS_EPS) and (abs(y-self.y)<POS_EPS):
        #     # check if angle is within threshold
        #     if abs(theta-self.theta)<THETA_EPS:
        #         return True
        #     # check if angle occured during rotation (between current and previous angle)
        #     elif (self.theta > theta) and self.prev_th_less_than_g:
        #         return True
        #     elif (self.theta < theta) and not self.prev_th_less_than_g:
        #         return True

        # return False

        return (abs(x-self.x)<POS_EPS and abs(y-self.y)<POS_EPS and abs(theta-self.theta)<THETA_EPS)

    def init_stop_sign(self):
        """ initiates a stop sign maneuver """

        self.stop_sign_start = rospy.get_rostime()

    def has_stopped(self):
        """ checks if stop sign maneuver is over """

        return (self.mode == Mode.STOP and (rospy.get_rostime()-self.stop_sign_start)>rospy.Duration.from_sec(STOP_TIME))

    def has_crossed(self):
        """ checks if crossing maneuver is over """
        current_pose = [self.x, self.y]
        dist2stop = []
        for i in range(self.stop_signs.locations.shape[0]):
            dist2stop.append(np.linalg.norm(current_pose - self.stop_signs.locations[i,:])) # Creates list of distances to all stop signs
        return (self.mode == Mode.CROSS and all(dist > CROSS_MIN_DIST for dist in dist2stop)) # (rospy.get_rostime()-self.cross_start)>rospy.Duration.from_sec(CROSSING_TIME))

    def pop_animal(self):
        # remove the animal from the rescue queue
        waypoint, animal_type = self.animal_waypoints.pop()
        print waypoint, animal_type

        if np.any(waypoint == None):
            pass
        else:
            self.x_g = waypoint[0]
            self.y_g = waypoint[1]
            self.theta_g = waypoint[2]
            self.target_animal = animal_type

    def init_rescue_animal(self):
        """ initiates an animal rescue """

        self.rescue_start = rospy.get_rostime()
        self.mode = Mode.RESCUE_ANIMAL

    def has_rescued(self):
        """checks if animal has been rescued"""

        return (self.mode == Mode.RESCUE_ANIMAL and (rospy.get_rostime()-self.rescue_start)>rospy.Duration.from_sec(RESCUE_TIME))

    def init_plan_rescue(self):
        print('init plan rescue')
        self.tsales_circuit_received = 0
        self.lock_animal_waypoints = 1

        print(self.animal_waypoints.poses)
        print(self.animal_waypoints.observations_count)
        self.animal_waypoints.cull(ANIMAL_MIN_OBSERVATIONS)
        print(self.animal_waypoints.poses)
        print(self.animal_waypoints.observations_count)

        if self.animal_waypoints.poses.shape[0] > 0:
            tsales_request = TSalesRequest()
            tsales_request.goal_x = self.animal_waypoints.poses[:,0].tolist()
            tsales_request.goal_y = self.animal_waypoints.poses[:,1].tolist()
            tsales_request.do_fast = 0
            print('publish tsales request')
            self.tsales_request_publisher.publish(tsales_request) 
        else: 
            self.tsales_circuit_received = 1

    def tsales_circuit_callback(self, msg): 
        print('tsales circuit callback')
        try:
            circuit = np.array(map(int, msg.circuit))
        except:
            rospy.loginfo('Traveling salesman failed')
            self.tsales_circuit_received = 1
            return

        if circuit.shape[0] == self.animal_waypoints.poses.shape[0]:
            self.animal_waypoints.reorder(circuit)
        else:         
            rospy.loginfo('Traveling salesman failed')

        self.tsales_circuit_received = 1

    def amcl_init_callback(self, msg):
        print('amcl init callback')
        # update pose
        self.x = msg.pose.pose.position.x
        self.y = msg.pose.pose.position.y
        rotation = [msg.pose.pose.orientation.x, msg.pose.pose.orientation.y, msg.pose.pose.orientation.z, msg.pose.pose.orientation.w]
        euler = tf.transformations.euler_from_quaternion(rotation)
        self.theta = euler[2]

        self.amcl_init_received = True

    def init_explore(self):
        print('init explore')
        self.explore_started = True

        # use the following for real robot
        exploration_target_waypoints = np.array([
            [10.604, 9.454, -0.996], # exit parking lot
            [10.098, 8.857, -2.633], # first stop sign on right
            [10.505, 7.226, -0.948], # end of one-way on right side
            [9.862, 6.676, -2.880], # look towards curved forest
            [8.724, 7.894, 2.169], # move down next to main intersection
            [9.584, 8.309, 0.387], # move next to angled stop sign
            [10.505, 7.226, -0.948], # end of one-way on right side
            [11.125, 6.987, 0.550], # natural turn left from one-way to side street
            [11.139, 8.821, 2.178], # move down next to main intersection
            [10.098, 8.857, -2.633], # back to stop sign on right
            [8.875, 8.163, -2.567], # move across main intersection
            [8.399, 8.897, 0.586] # move towards parking lot
            # [8.756, 9.288, -0.996] # park

            # Position(10.447, 7.391, 0.000), Orientation(0.000, 0.000, -0.478, 0.879) = Angle: -0.996
            # [9.798, 8.728, -2.544],
            # [10.702, 7.249, -0.936],
            # [9.874, 6.624, 2.508],
            # [8.726, 7.992,  2.057],
            # [10.449, 9.007, 0.483],
            # [11.586, 7.830, -1.044],
            # [10.491, 6.584, -3.071],
            # [8.680, 8.073, 2.155]

            # [0.4, 0.3, np.pi/2],
            ])

        # use the following for simulation
        # exploration_target_waypoints = np.array([
        #     [3.4, 2.8, np.pi/2],
        #     [2.5, 1.5, np.pi],
        #     [0.5, 1.6, np.pi],
        #     [0.3, 0.3, 0],
        #     [3.3, 0.3, np.pi/2]
        #     ])

        for i in range(len(exploration_target_waypoints)):
            self.explore_waypoints.add_exploration_waypoint(exploration_target_waypoints[i, :])

    def pop_explore_waypoint(self):
        # remove the exploration waypoint from the exploration queue
        waypoint = self.explore_waypoints.pop()
        print waypoint

        if np.any(waypoint == None):
            pass
        else:
            self.x_g = waypoint[0]
            self.y_g = waypoint[1]
            self.theta_g = waypoint[2]
            
            # Save prior goal pose for animal detection
            self.pose_goal_backlog = [self.x_g, self.y_g, self.theta_g]

    def loop(self):
        """ the main loop of the robot. At each iteration, depending on its
        mode (i.e. the finite state machine's state), if takes appropriate
        actions. This function shouldn't return anything """

        try:
            (translation,rotation) = self.trans_listener.lookupTransform('/map', '/base_footprint', rospy.Time(0))
            self.x = translation[0]
            self.y = translation[1]
            euler = tf.transformations.euler_from_quaternion(rotation)
            self.theta = euler[2]

            if self.theta < self.theta_g:
                self.prev_th_less_than_g = True

        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
            pass

        #self.bicycles.publish_all()
        self.stop_signs.publish_all()
        self.animal_waypoints.publish_all()

        # logs the current mode
        if not(self.last_mode_printed == self.mode):
            rospy.loginfo("Current Mode: %s", self.mode)
            self.last_mode_printed = self.mode
            self.init_flag = 0

        # checks wich mode it is in and acts accordingly
        if self.mode == Mode.IDLE:
            if self.amcl_init_received == True:
                self.amcl_init_received = False
                self.mode = Mode.EXPLORE
                # self.mode = Mode.NAV

            else:
                # send zero velocity
                self.stay_idle()

        elif self.mode == Mode.BIKE_STOP:
            if self.honk:

                if (self.playsound):
                    self.soundhandle.playWave('/home/aa274/catkin_ws/src/asl_turtlebot/BICYCLE.wav', 1.0)
                    self.playsound = False
                    print("Playing the sound")

            if (rospy.get_rostime() - self.bike_detected_start > rospy.Duration.from_sec(BIKE_STOP_TIME)):
                self.honk = False
                self.playsound = True
                print("I'm stopping the honking")
                self.mode = self.modeafterhonk
            else:
                self.stay_idle()

        elif self.mode == Mode.STOP:
            # at a stop sign

            if not self.init_flag:
                self.init_flag = 1
                self.init_stop_sign()

            if self.has_stopped():
                self.mode = Mode.CROSS
            else:
                self.stay_idle()

        elif self.mode == Mode.CROSS:
            # crossing an intersection

            if self.has_crossed():
                self.mode = self.modeafterstop
            else:
                self.nav_to_pose()

            if self.close_to(self.x_g,self.y_g,self.theta_g):
                if self.modeafterstop == Mode.NAV:
                    self.mode = Mode.IDLE
                elif self.modeafterstop == Mode.GO_TO_ANIMAL:
                    self.mode = Mode.RESCUE_ANIMAL

            if self.honk:
                self.mode = Mode.BIKE_STOP
                self.modeafterhonk = Mode.CROSS

        elif self.mode == Mode.NAV:
            self.lock_animal_waypoints = 0

            if self.close_to(self.x_g,self.y_g,self.theta_g):
                self.mode = Mode.GO_TO_EXPLORE_WAYPOINT
            else:
                if self.stop_check(): # Returns True if within STOP_MIN_DIST
                    self.mode = Mode.STOP
                    self.modeafterstop = Mode.NAV
                else:
                    self.nav_to_pose()

            if self.honk:
                self.mode = Mode.BIKE_STOP
                self.modeafterhonk = Mode.NAV
            
        elif self.mode == Mode.PLAN_RESCUE:
            self.stay_idle()

            if not self.init_flag:
                self.init_plan_rescue()
                self.init_flag = 1
            
            if self.tsales_circuit_received:
                self.mode = Mode.REQUEST_RESCUE

        elif self.mode == Mode.REQUEST_RESCUE:
            # publish message that rescue is ready
            rescue_ready_msg = True
            self.rescue_ready_publisher.publish(rescue_ready_msg)

            if(self.play_rescue_sound):
                "plY SONG"
                self.soundhandle.playWave('/home/aa274/catkin_ws/src/asl_turtlebot/finalcount.wav', 1.0)
                self.play_rescue_sound = False;

            # when rescue on message is received, tranisition to rescue
            if self.rescue_on:
                if self.animal_waypoints.length() > 0:
                    self.pop_animal()
                    self.mode = Mode.GO_TO_ANIMAL
                else:
                    self.mode = Mode.IDLE

        elif self.mode == Mode.GO_TO_ANIMAL:
            # navigate to the animal
            if self.close_to(self.x_g,self.y_g,self.theta_g):
                self.mode = Mode.RESCUE_ANIMAL
            else:
                if self.stop_check(): # Returns True if within STOP_MIN_DIST
                    self.mode = Mode.STOP
                    self.modeafterstop = Mode.GO_TO_ANIMAL
                else:
                    self.nav_to_pose()

            if self.honk:
                self.mode = Mode.BIKE_STOP
                self.modeafterhonk = Mode.GO_TO_ANIMAL

        elif self.mode == Mode.RESCUE_ANIMAL:
            if not self.init_flag:
                self.init_flag = 1
                self.init_rescue_animal()

            if self.has_rescued():
                rospy.loginfo("Rescued a: %s", self.target_animal)
                if self.animal_waypoints.length() > 0:
                    self.pop_animal()
                    self.mode = Mode.GO_TO_ANIMAL
                else:
                    self.mode = Mode.VICTORY

        elif self.mode == Mode.EXPLORE:
            if self.explore_started == False:
                self.init_explore()
            
            if self.explore_waypoints.length() > 0:
                self.pop_explore_waypoint()
                self.mode = Mode.GO_TO_EXPLORE_WAYPOINT

            else:
                self.mode = Mode.PLAN_RESCUE

        elif self.mode == Mode.GO_TO_EXPLORE_WAYPOINT:
            # check if rotating to animal or normal exploration
            if not rospy.get_rostime().to_sec() > self.time_of_spin + MAX_PINPOINT_TIME:
                if self.nav_to_animal_CW:
                    self.turn_CW()
                elif self.nav_to_animal_CCW:
                    self.turn_CCW()
            else:
                # navigate to the exploration waypoint
                if self.close_to(self.x_g,self.y_g,self.theta_g):
                    # if self.nav_to_animal:
                    #     self.mode = Mode.GO_TO_EXPLORE_WAYPOINT
                    self.mode = Mode.EXPLORE
                else:
                    if self.stop_check(): # Returns True if within STOP_MIN_DIST
                        self.mode = Mode.STOP
                        self.modeafterstop = Mode.GO_TO_EXPLORE_WAYPOINT
                    else:
                        # print self.x_g, self.y_g, self.theta_g
                        self.nav_to_pose()

            if self.honk:
                self.mode = Mode.BIKE_STOP
                self.modeafterhonk = Mode.GO_TO_EXPLORE_WAYPOINT

        elif self.mode == Mode.VICTORY:
            # self.stay_idle()

            if(self.victory_sound):
                self.soundhandle.playWave('/home/aa274/catkin_ws/src/asl_turtlebot/victory.wav', 1.0)
                self.victory_sound = False

            twist = Twist()
            twist.linear.x = 0; twist.linear.y = 0; twist.linear.z = 0
            twist.angular.x = 0; twist.angular.y = 0; twist.angular.z = 10.0
            self.cmd_vel_publisher.publish(twist)

        else:
            raise Exception('This mode is not supported: %s'
                % str(self.mode))

    def run(self):
        rate = rospy.Rate(10) # 10 Hz
        while not rospy.is_shutdown():
            self.loop()
            rate.sleep()

if __name__ == '__main__':
    sup = Supervisor()
    sup.run()
