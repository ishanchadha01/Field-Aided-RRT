from __future__ import division
from shapely.geometry import Point, LineString
import random
import math
import numpy as np


class RRTFamilyPathPlanner():

    """Plans path using an algorithm from the RRT family.

    Contains methods for simple RRT based search, RRTstar based search and informed RRTstar based search.

    """

    def initialise(self, environment, bounds, start_pose, goal_region, object_radius, steer_distance, num_iterations, resolution, runForFullIterations):
        """Initialises the planner with information about the environment and parameters for the rrt path planers

        Args:
            environment (A yaml environment): Environment where the planner will be run. Includes obstacles.
            bounds( (int int int int) ): min x, min y, max x, and max y coordinates of the bounds of the world.
            start_pose( (float float) ): Starting x and y coordinates of the object in question.
            goal_region (Polygon): A polygon representing the region that we want our object to go to.
            object_radius (float): Radius of the object.
            steer_distance (float): Limits the length of the branches
            num_iterations (int): How many points are sampled for the creationg of the tree
            resolution (int): Number of segments used to approximate a quarter circle around a point.
            runForFullIterations (bool): If True RRT and RRTStar return the first path found without having to sample all num_iterations points.

        Returns:
            None
        """
        self.env = environment
        self.obstacles = environment.obstacles
        self.bounds = bounds
        self.minx, self.miny, self.maxx, self.maxy = bounds
        self.start_pose = start_pose
        self.goal_region = goal_region
        self.obj_radius = object_radius
        self.N = num_iterations
        self.resolution = resolution
        self.steer_distance = steer_distance
        self.V = set()
        self.E = set()
        self.child_to_parent_dict = dict() #key = child, value = parent
        self.runForFullIterations = runForFullIterations
        self.goal_pose = (goal_region.centroid.coords[0])


    def path(self, environment, bounds, start_pose, goal_region, object_radius, steer_distance, num_iterations, resolution, runForFullIterations, RRT_Flavour):
        """Returns a path from the start_pose to the goal region in the current environment using the specified RRT-variant algorithm.

        Args:
            environment (A yaml environment): Environment where the planner will be run. Includes obstacles.
            bounds( (int int int int) ): min x, min y, max x, and max y coordinates of the bounds of the world.
            start_pose( (float float) ): Starting x and y coordinates of the object in question.
            goal_region (Polygon): A polygon representing the region that we want our object to go to.
            object_radius (float): Radius of the object.
            steer_distance (float): Limits the length of the branches
            num_iterations (int): How many points are sampled for the creationg of the tree
            resolution (int): Number of segments used to approximate a quarter circle around a point.
            runForFullIterations (bool): If True RRT and RRTStar return the first path found without having to sample all num_iterations points.
            RRT_Flavour (str): A string representing what type of algorithm to use.
                               Options are 'RRT', 'RRT*', and 'InformedRRT*'. Anything else returns None,None,None.

        Returns:
            path (list<(int,int)>): A list of tuples/coordinates representing the nodes in a path from start to the goal region
            self.V (set<(int,int)>): A set of Vertices (coordinates) of nodes in the tree
            self.E (set<(int,int),(int,int)>): A set of Edges connecting one node to another node in the tree
        """
        self.env = environment

        self.initialise(environment, bounds, start_pose, goal_region, object_radius, steer_distance, num_iterations, resolution, runForFullIterations)

        # Define start and goal in terms of coordinates. The goal is the centroid of the goal polygon.
        x0, y0 = start_pose
        x1, y1 = goal_region.centroid.coords[0]
        start = (x0, y0)
        goal = (x1, y1)

        # Handle edge case where where the start is already at the goal
        if start == goal:
            path = [start, goal]
            self.V.union([start, goal])
            self.E.union([(start, goal)])
        # There might also be a straight path to goal, consider this case before invoking algorithm
        elif self.isEdgeCollisionFree(start, goal):
            path = [start, goal]
            self.V.union([start, goal])
            self.E.union([(start, goal)])
        # Run the appropriate RRT algorithm according to RRT_Flavour
        else:
            if RRT_Flavour == "RRT":
                path, self.V, self.E = self.RRTSearch()
            elif RRT_Flavour == "RRT*":
                path, self.V, self.E = self.RRTStarSearch()
            elif RRT_Flavour == "InformedRRT*":
                path, self.V, self.E = self.InformedRRTStarSearch()
            else:
                # The RRT flavour has no defined algorithm, therefore return None for all values
                return None, None, None

        return path, self.V, self.E


    def RRTSearch(self):
        """Returns path using RRT algorithm.

        Builds a tree exploring from the start node until it reaches the goal region. It works by sampling random points in the map and connecting them with
        the tree we build off on each iteration of the algorithm.

        Returns:
            path (list<(int,int)>): A list of tuples/coordinates representing the nodes in a path from start to the goal region
            self.V (set<(int,int)>): A set of Vertices (coordinates) of nodes in the tree
            self.E (set<(int,int),(int,int)>): A set of Edges connecting one node to another node in the tree
        """

        # Initialize path and tree to be empty.
        path = []
        path_length = float('inf')
        tree_size = 0
        path_size = 0
        self.V.add(self.start_pose)
        goal_centroid = self.get_centroid(self.goal_region)

        # Iteratively sample N random points in environment to build tree
        for i in range(self.N):
            if(random.random()>=1.95): # Change to a value under 1 to bias search towards goal, right now this line doesn't run
                random_point = goal_centroid
            else:
                random_point = self.get_collision_free_random_point()

            # The new point to be added to the tree is not the sampled point, but a colinear point with it and the nearest point in the tree.
            # This keeps the branches short
            nearest_point = self.find_nearest_point(random_point)
            new_point = self.steer(nearest_point, random_point)

            # If there is no obstacle between nearest point and sampled point, add the new point to the tree.
            if self.isEdgeCollisionFree(nearest_point, new_point):
                self.V.add(new_point)
                self.E.add((nearest_point, new_point))
                self.setParent(nearest_point, new_point)
                # If new point of the tree is at the goal region, we can find a path in the tree from start node to goal.
                if self.isAtGoalRegion(new_point):
                    if not self.runForFullIterations: # If not running for full iterations, terminate as soon as a path is found.
                        path, tree_size, path_size, path_length = self.find_path(self.start_pose, new_point)
                        break
                    else: # If running for full iterations, we return the shortest path found.
                        tmp_path, tmp_tree_size, tmp_path_size, tmp_path_length = self.find_path(self.start_pose, new_point)
                        if tmp_path_length < path_length:
                            path_length = tmp_path_length
                            path = tmp_path
                            tree_size = tmp_tree_size
                            path_size = tmp_path_size

        # If no path is found, then path would be an empty list.
        return path, self.V, self.E


    def RRTStarSearch(self):
        """Returns path using RRTStar algorithm.

        Uses the same structure as RRTSearch, except there's an additional 'rewire' call when adding nodes to the tree.
        This can be seen as a way to optimise the branches of the subtree where the new node is being added.

        Returns:
            path (list<(int,int)>): A list of tuples/coordinates representing the nodes in a path from start to the goal region
            self.V (set<(int,int)>): A set of Vertices (coordinates) of nodes in the tree
            self.E (set<(int,int),(int,int)>): A set of Edges connecting one node to another node in the tree
        """
        # Code is very similar to RRTSearch, so for simplicity's sake only the main differences have been commented.
        path = []
        path_length = float('inf')
        tree_size = 0
        path_size = 0
        self.V.add(self.start_pose)
        goal_centroid = self.get_centroid(self.goal_region)

        for i in range(self.N):
            if(random.random()>=1.95):
                random_point = goal_centroid
            else:
                random_point = self.get_collision_free_random_point()

            nearest_point = self.find_nearest_point(random_point)
            new_point = self.steer(nearest_point, random_point)

            if self.isEdgeCollisionFree(nearest_point, new_point):
                # Find the nearest set of points around the new point
                nearest_set = self.find_nearest_set(new_point)
                min_point = self.find_min_point(nearest_set, nearest_point, new_point)
                self.V.add(new_point)
                self.E.add((min_point, new_point))
                self.setParent(min_point, new_point)
                # Main difference between RRT and RRT*, modify the points in the nearest set to optimise local path costs.
                self.rewire(nearest_set, min_point, new_point)
                if self.isAtGoalRegion(new_point):
                    if not self.runForFullIterations:
                        path, tree_size, path_size, path_length = self.find_path(self.start_pose, new_point)
                        break
                    else:
                        tmp_path, tmp_tree_size, tmp_path_size, tmp_path_length = self.find_path(self.start_pose, new_point)
                        if tmp_path_length < path_length:
                            path_length = tmp_path_length
                            path = tmp_path
                            tree_size = tmp_tree_size
                            path_size = tmp_path_size

        return path, self.V, self.E


    def InformedRRTStarSearch(self):
        """Returns path using informed RRTStar algorithm.

        Uses the same structure as RRTStarSearch, except that once a path is found, sampling is restricted to an ellipse
        containing the shortest path found.

        Returns:
            path (list<(int,int)>): A list of tuples/coordinates representing the nodes in a path from start to the goal region
            self.V (set<(int,int)>): A set of Vertices (coordinates) of nodes in the tree
            self.E (set<(int,int),(int,int)>): A set of Edges connecting one node to another node in the tree
        """
        # Code is very similar to RRTStarSearch, so for simplicity's sake only the main differences have been commented.
        path = []
        path_length = float('inf')
        c_best = float('inf') # Max length we expect to find in our 'informed' sample space starts as infinite
        tree_size = 0
        path_size = 0
        self.V.add(self.start_pose)
        goal_centroid = self.get_centroid(self.goal_region)
        solution_set = set()

        start_obj = Point(self.start_pose).buffer(self.obj_radius, self.resolution)
        # The following equations define the space of the environment that is being sampled.
        c_min = start_obj.distance(self.goal_region)
        x_center = np.matrix([[(self.start_pose[0] + self.goal_pose[0]) / 2.0],[(self.start_pose[1] + self.goal_pose[1]) / 2.0], [0]])
        a_1 = np.matrix([[(self.goal_pose[0] - self.start_pose[0]) / c_min],[(self.goal_pose[1] - self.start_pose[1]) / c_min], [0]])
        id1_t = np.matrix([1.0,0,0])
        M = np.dot(a_1, id1_t)
        U,S,Vh = np.linalg.svd(M, 1, 1)
        C = np.dot(np.dot(U, np.diag([1.0,1.0,  np.linalg.det(U) * np.linalg.det(np.transpose(Vh))])), Vh)

        for i in range(self.N):
            # The main difference in this algorithm is that we limit our sample space.
            # Sample space is defined by c_best (our best path and the maximum path length inside the new space)
            # c_min (the distance between start and goal), x_center (midpoint between start and goal) and C
            # only c_best changes whenever a new path is found.
            random_point = self.sample(c_best, c_min, x_center, C)

            nearest_point = self.find_nearest_point(random_point)
            new_point = self.steer(nearest_point, random_point)

            if self.isEdgeCollisionFree(nearest_point, new_point):
                nearest_set = self.find_nearest_set(new_point)
                min_point = self.find_min_point(nearest_set, nearest_point, new_point)
                self.V.add(new_point)
                self.E.add((min_point, new_point))
                self.setParent(min_point, new_point)
                self.rewire(nearest_set, min_point, new_point)
                if self.isAtGoalRegion(new_point):
                    solution_set.add(new_point)
                    tmp_path, tmp_tree_size, tmp_path_size, tmp_path_length = self.find_path(self.start_pose, new_point)
                    if tmp_path_length < path_length:
                        path_length = tmp_path_length
                        path = tmp_path
                        tree_size = tmp_tree_size
                        path_size = tmp_path_size
                        c_best = tmp_path_length # c_best is calculated everytime a path is found. Affecting the sample space.

        return path, self.V, self.E


    """
    ******************************************************************************************************************************************
    ***************************************************** Helper Functions *******************************************************************
    ******************************************************************************************************************************************
    """

    def sample(self, c_max, c_min, x_center, C):
        if c_max < float('inf'):
            r= [c_max /2.0, math.sqrt(c_max**2 - c_min**2)/2.0, math.sqrt(c_max**2 - c_min**2)/2.0]
            L = np.diag(r)
            x_ball = self.sample_unit_ball()
            random_point = np.dot(np.dot(C,L), x_ball) + x_center
            random_point = (random_point[(0,0)], random_point[(1,0)])
        else:
            random_point = self.get_collision_free_random_point()
        return random_point

    def sample_unit_ball(self):
        a = random.random()
        b = random.random()

        if b < a:
            tmp = b
            b = a
            a = tmp
        sample = (b*math.cos(2*math.pi*a/b), b*math.sin(2*math.pi*a/b))
        return np.array([[sample[0]], [sample[1]], [0]])

    def find_nearest_set(self, new_point):
        points = set()
        ball_radius = self.find_ball_radius()
        for vertex in self.V:
            euc_dist = self.euclidian_dist(new_point, vertex)
            if euc_dist < ball_radius:
                points.add(vertex)
        return points

    def find_ball_radius(self):
        unit_ball_volume = math.pi
        n = len(self.V)
        dimensions = 2.0
        gamma = (2**dimensions)*(1.0 + 1.0/dimensions) * (self.maxx - self.minx) * (self.maxy - self.miny)
        ball_radius = min(((gamma/unit_ball_volume) * math.log(n) / n)**(1.0/dimensions), self.steer_distance)
        return ball_radius


    def find_min_point(self, nearest_set, nearest_point, new_point):
        min_point = nearest_point
        min_cost = self.cost(nearest_point) + self.linecost(nearest_point, new_point)
        for vertex in nearest_set:
            if self.isEdgeCollisionFree(vertex, new_point):
                temp_cost = self.cost(vertex) + self.linecost(vertex, new_point)
                if temp_cost < min_cost:
                    min_point = vertex
                    min_cost = temp_cost
        return min_point

    def rewire(self, nearest_set, min_point, new_point):
        # Discards edges in the nearest_set that lead to a longer path than going through the new_point first
        # Then add an edge from new_point to the vertex in question and update its parent accordingly.
        for vertex in nearest_set - set([min_point]):
            if self.isEdgeCollisionFree(vertex, new_point):
                if self.cost(vertex) > self.cost(new_point) + self.linecost(vertex, new_point):
                    parent_point = self.getParent(vertex)
                    self.E.discard((parent_point, vertex))
                    self.E.discard((vertex, parent_point))
                    self.E.add((new_point, vertex))
                    self.setParent(new_point, vertex)


    def cost(self, vertex):
        path, tree_size, path_size, path_length = self.find_path(self.start_pose, vertex)
        return path_length


    def linecost(self, point1, point2):
        return self.euclidian_dist(point1, point2)

    def getParent(self, vertex):
        return self.child_to_parent_dict[vertex]

    def setParent(self, parent, child):
        self.child_to_parent_dict[child] = parent

    def get_random_point(self):
        x = self.minx + random.random() * (self.maxx - self.minx)
        y = self.miny + random.random() * (self.maxy - self.miny)
        return (x, y)

    def get_collision_free_random_point(self):
        # Run until a valid point is found
        while True:
            point = self.get_random_point()
            # Pick a point, if no obstacle overlaps with a circle centered at point with some obj_radius then return said point.
            buffered_point = Point(point).buffer(self.obj_radius, self.resolution)
            if self.isPointCollisionFree(buffered_point):
                return point

    def isPointCollisionFree(self, point):
        for obstacle in self.obstacles:
            if obstacle.contains(point):
                return False
        return True

    def find_nearest_point(self, random_point):
        closest_point = None
        min_dist = float('inf')
        for vertex in self.V:
            euc_dist = self.euclidian_dist(random_point, vertex)
            if euc_dist < min_dist:
                min_dist = euc_dist
                closest_point = vertex
        return closest_point

    def isOutOfBounds(self, point):
        if((point[0] - self.obj_radius) < self.minx):
            return True
        if((point[1] - self.obj_radius) < self.miny):
            return True
        if((point[0] + self.obj_radius) > self.maxx):
            return True
        if((point[1] + self.obj_radius) > self.maxy):
            return True
        return False


    def isEdgeCollisionFree(self, point1, point2):
        if self.isOutOfBounds(point2):
            return False
        line = LineString([point1, point2])
        expanded_line = line.buffer(self.obj_radius, self.resolution)
        for obstacle in self.obstacles:
            if expanded_line.intersects(obstacle):
                return False
        return True

    def steer(self, from_point, to_point):
        fromPoint_buffered = Point(from_point).buffer(self.obj_radius, self.resolution)
        toPoint_buffered = Point(to_point).buffer(self.obj_radius, self.resolution)
        if fromPoint_buffered.distance(toPoint_buffered) < self.steer_distance:
            return to_point
        else:
            from_x, from_y = from_point
            to_x, to_y = to_point
            theta = math.atan2(to_y - from_y, to_x- from_x)
            new_point = (from_x + self.steer_distance * math.cos(theta), from_y + self.steer_distance * math.sin(theta))
            return new_point

    def isAtGoalRegion(self, point):
        buffered_point = Point(point).buffer(self.obj_radius, self.resolution)
        intersection = buffered_point.intersection(self.goal_region)
        inGoal = intersection.area / buffered_point.area
        return inGoal >= 0.5

    def euclidian_dist(self, point1, point2):
        return math.sqrt((point2[0] - point1[0])**2 + (point2[1] - point1[1])**2)

    def find_path(self, start_point, end_point):
        # Returns a path by backtracking through the tree formed by one of the RRT algorithms starting at the end_point until reaching start_node.
        path = [end_point]
        tree_size, path_size, path_length = len(self.V), 1, 0
        current_node = end_point
        previous_node = None
        target_node = start_point
        while current_node != target_node:
            parent = self.getParent(current_node)
            path.append(parent)
            previous_node = current_node
            current_node = parent
            path_length += self.euclidian_dist(current_node, previous_node)
            path_size += 1
        path.reverse()
        return path, tree_size, path_size, path_length

    def get_centroid(self, region):
        centroid = region.centroid.wkt
        filtered_vals = centroid[centroid.find("(")+1:centroid.find(")")]
        filtered_x = filtered_vals[0:filtered_vals.find(" ")]
        filtered_y = filtered_vals[filtered_vals.find(" ") + 1: -1]
        (x,y) = (float(filtered_x), float(filtered_y))
        return (x,y)