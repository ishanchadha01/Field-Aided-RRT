import argparse
from collections import defaultdict
import os
from copy import deepcopy
import random
import math
import shutil
import tkinter as tk
import numpy as np
from scipy.spatial import KDTree
from queue import PriorityQueue
from matplotlib import pyplot as plt

from shapely.geometry.base import BaseGeometry
from shapely.geometry import Point, LineString, MultiPoint, MultiPolygon
from shapely.ops import nearest_points

from farrt.PartiallyObservablePlanner import PartiallyObservablePlanner
from farrt.node import Node
from farrt.plot import plot_polygons, plot_planner
from farrt.world import World
from farrt.utils import as_multipoint, multipoint_without, pt2tuple, shapely_edge

import imageio

vertex_t = tuple[float,float]
edge_t = tuple[vertex_t,vertex_t]

class RRTStar(PartiallyObservablePlanner):

  def __init__(self, *args, **kwargs) -> None:
    super().__init__(*args, **kwargs)
    self.iters = kwargs.get('iters', 2000)
    self.eps = kwargs.get('eps', .01)

    self.steer_distance = kwargs.get('max_step_length', self.vision_radius / 3)

    self.obstacle_avoidance_radius = kwargs.get('obstacle_avoidance_radius', self.steer_distance * 2/3)

    # get shapely Point version of start/goal
    self.x_start_pt = self.x_start.coord
    self.x_goal_pt = self.x_goal.coord

    self.rrt_tree: MultiPoint = MultiPoint()
    self.rrt_vertices: set[vertex_t] = set()
    self.rrt_edges: set[edge_t] = set()
    self.child_to_parent_map: dict[vertex_t, vertex_t] = {}
    self.parent_to_children_map: defaultdict[vertex_t, set[vertex_t]] = defaultdict(set)
    self.cost_to_reach: dict[vertex_t, float] = {}

    self.free_points: MultiPoint = MultiPoint()

    self.goal_reached_thresh = 1
    self.built_tree = False


  def handle_new_obstacles(self, new_obstacles: BaseGeometry) -> None:
    if not self.built_tree: # build the tree on first observations
      self.do_first_plan()
      return
    if new_obstacles.is_empty: # ignore step if no new obstacles discovered
      return
    if not len(self.planned_path): # ignore step if no path planned (may be an error case)
      print('No path planned!')
      return
    # if we have a path, check if we need to replan

    # get the full remaining path including current position
    path = [self.curr_pos.coord] + [node.coord for node in self.planned_path]
    path_line = LineString(path)

    # check if the remaining path to goal intersects with the new obstacles
    if path_line.intersects(new_obstacles):
      print('Path is inconsistent with new obstacles')
      # print(path_line)
      intersections = path_line.intersection(new_obstacles)
      # print(intersections)
      # print(new_obstacles)
      self.replan(new_obstacles=new_obstacles, draw_intersections=intersections)

  def handle_deleted_obstacles(self, deleted_obstacles: BaseGeometry) -> None:
      return super().handle_deleted_obstacles(deleted_obstacles)

  def update_plan(self) -> None:
    """
    RRT* does not have any default update process after each step.
    """
    return super().update_plan()

  def do_first_plan(self) -> None:
    """
    Build the RRT tree on the first step.
    Extract a plan from the tree.
    """
    # empty out the current planned path (should already be empty)
    self.planned_path = []
    print(f'Do first rrt plan! {pt2tuple(self.x_start_pt)} -> {pt2tuple(self.x_goal_pt)}')

    # build the tree from goal to start
    final_pt,final_cost = self.build_rrt_tree(root=self.x_goal_pt, goal_pt=self.curr_pos.coord, goal_threshold=0)
    self.built_tree = True
    print(f'First plan complete. Ends at {final_pt} with cost {final_cost}')

    # extract a plan from the tree and reverse it (to go from goal to start)
    self.planned_path = self.extract_path(endpoint=final_pt,root=self.x_goal_pt,reverse=True)
    print(f'Path: {len(self.planned_path)}')

    # display the initial plan
    # if self.gui:
    #   self.render(visualize=True)

  def replan(self, new_obstacles: MultiPolygon, **kwargs):
    """
    Replan the path from the current position to the goal.
    1. Rerun RRT* from current position to goal
    2. Extract a plan from the tree
    """
    print('Planning...')
    if self.gui:
      self.render(draw_world_obstacles=False, save_frame=True, **kwargs)

    self.planned_path = []
    final_pt,final_cost = self.build_rrt_tree(root=self.x_goal_pt, goal_pt=self.curr_pos.coord, goal_threshold=0)
    self.planned_path = self.extract_path(endpoint=final_pt,root=self.x_goal_pt,reverse=True)

    print('Planning complete')
    # if self.gui:
    #   self.render()

  def sample_free(self, goal_pt: Point, buffer_radius:float = None) -> Point:
    """
    Sample a free point in the world
    Return the goal with some low probability
    Ensure the new point is further than the buffer radius from existing obstacles
    """
    if random.random() < self.eps: # some % chance of returning goal node
      return goal_pt
    
    # use default buffer for obstacle avoidance
    if buffer_radius is None:
      buffer_radius = self.obstacle_avoidance_radius
    
    # sample a random point from the world
    rand_pos = self.world.random_position()

    # ensure that sampled point is not too close to detected obstacles
    while self.detected_obstacles.intersects(rand_pos.buffer(buffer_radius)):
      rand_pos = self.world.random_position()
    return rand_pos

  def find_nearest(self, x_rand: Point) -> Point:
    """
    Find the nearest point in the tree to the random point
    """
    nearest_geoms = nearest_points(multipoint_without(self.rrt_tree, x_rand), x_rand)
    nearest_pt = nearest_geoms[0]
    return nearest_pt

  def steer(self, x_nearest: Point, x_rand: Point) -> Point:
    """
    Steer from the nearest point to the random point
    Limit the step to the steer distance of the planner
    """
    dist = x_nearest.distance(x_rand)
    if dist == 0: # if the points are the same, just return the sampled point
      return x_rand
    
    # factor is max value = 1 (allow steps shorter than steer_distance if sampled point is very close to nearest)
    factor = min(1, self.steer_distance / dist)

    # interpolate along the line between the nearest and sampled points
    newCoord = shapely_edge(x_nearest,x_rand).interpolate(factor, normalized=True)
    return newCoord

  def edge_obstacle_free(self, x_nearest: Point, x_new: Point) -> bool:
    """
    Check if the edge between the nearest and new points is free of obstacles
    """
    return not self.detected_obstacles.intersects(shapely_edge(x_nearest,x_new))

  def find_nearby_pts(self, x_new: Point) -> MultiPoint:
    """
    Find all points in the tree within some radius of the new point
    Used for finding shortest paths and rewiring
    """
    def find_ball_radius():
      """
      Determine the radius of the ball to search for nearby points
      """
      unit_volume = math.pi
      num_vertices = len(self.rrt_vertices)
      dimensions = len(self.world.dims)
      minx,miny,maxx,maxy = self.world.getBounds()
      gamma = (2**dimensions)*(1.0 + 1.0/dimensions) * (maxx - minx) * (maxy - miny)
      ball_radius = min(
        ((gamma/unit_volume) * math.log(num_vertices) / num_vertices)**(1.0/dimensions),
        self.steer_distance
      )
      return ball_radius
    
    nearby_points = multipoint_without(self.rrt_tree, x_new).intersection(x_new.buffer(find_ball_radius()))
    return as_multipoint(nearby_points)

  def get_min_cost_point(self, nearby_pts: MultiPoint, x_nearest: Point, x_new: Point) -> Point:
    """
    Returns the point in nearby_pts that has the minimum cost to get to x_new
    defaults to x_nearest
    """
    min_point = x_nearest
    min_cost = self.get_cost_to_reach(x_nearest) + self.get_edge_cost(x_nearest, x_new)
    for point in nearby_pts.geoms:
      if self.edge_obstacle_free(point, x_new):
        temp_cost = self.get_cost_to_reach(point) + self.get_edge_cost(point, x_new)
        if temp_cost < min_cost:
          min_point = point
          min_cost = temp_cost
    return min_point,min_cost

  def add_start_vertex(self, x_start: Point) -> None:
    """
    Add the start vertex to the tree (no parents, 0 cost)
    """
    self.rrt_tree = self.rrt_tree.union(x_start)

    vtx = pt2tuple(x_start)

    self.rrt_vertices.add(vtx)
    
    self.set_cost_to_reach(x_start, 0)

  def add_vertex(self, /, pt: Point, parent: Point, cost: float) -> None:
    """
    Add a vertex to the tree, add edge from parent to new vertex, set cost to reach
    """
    self.rrt_tree = self.rrt_tree.union(pt)

    vtx = pt2tuple(pt)
    parent_vtx = pt2tuple(parent)

    self.rrt_vertices.add(vtx)
    self.rrt_edges.add((parent_vtx,vtx))

    self.set_child_parent(child=pt, parent=parent)
    self.set_cost_to_reach(pt, cost)

  def reassign_parent(self, /,*, pt: Point, parent: Point, cost: float, allow_same_parent:bool = False) -> None:
    """
    Reassign the parent of a vertex to a new parent, update cost to reach
    Remove old edges from the previous parent if present
    """
    prev_parent = self.get_parent(pt)

    if prev_parent == parent:
      if allow_same_parent:
        return
      else:
        raise ValueError(f'Cannot reassign parent to same parent - child:{pt} -> prev:{prev_parent} == new:{parent}')

    vtx = pt2tuple(pt)
    old_parent_vtx = pt2tuple(prev_parent)
    new_parent_vtx = pt2tuple(parent)

    self.rrt_edges.discard((old_parent_vtx,vtx))
    self.rrt_edges.discard((vtx,old_parent_vtx))
    self.rrt_edges.add((new_parent_vtx,vtx))
    
    self.set_child_parent(child=pt, parent=parent)
    if vtx in self.get_children(prev_parent):
      print(f'Failed to remove child {pt} from parent {prev_parent} - {self.get_children(prev_parent)}')
      print(f'Sever failure: {self.parent_to_children_map[pt2tuple(prev_parent)]}')
      print(prev_parent == parent)
    assert vtx not in self.get_children(prev_parent)
    self.set_cost_to_reach(pt, cost)

  def do_rrtstar_rewiring(self, nearby_pts: MultiPoint, x_min: Point, x_new: Point) -> None:
    """
    Rewire the edges of the tree to connect x_new first if it is closer than the current parent
    Discards edges from points in nearby_pts if they can be improved by going thorugh x_new
      Then adds an edge from x_new to the point in nearby_pts (and update parent)
    """
    for x_nearby in multipoint_without(nearby_pts, x_min).geoms:
      if self.edge_obstacle_free(x_new, x_nearby):
        cost_with_new = self.get_cost_to_reach(x_new) + self.get_edge_cost(x_new, x_nearby)
        if self.get_cost_to_reach(x_nearby) > cost_with_new:
          # allow reassigning to same parent if new node is current position (b/c curr pos may be sampled many times)
          self.reassign_parent(pt=x_nearby, parent=x_new, cost=cost_with_new, allow_same_parent=x_new == self.curr_pos.coord)

  def reached_goal(self, x_new: Point, *, goal: Point = None, threshold:float = None) -> bool:
    """
    Check if the new point is close enough to the goal to be considered reached
    """
    if goal is None: # default to the actual goal of the planner
      goal = self.x_goal_pt
    if threshold is None:
      threshold = self.goal_reached_thresh
    
    # check that the distance is below the threshold
    return x_new.distance(goal) < self.goal_reached_thresh

  def build_rrt_tree(self, *, root: Point, goal_pt: Point, goal_threshold:float = None) -> None:
    """
    Builds the rrt tree from the root to the goal
    """
    if goal_threshold is None:
      goal_threshold = self.goal_reached_thresh
    
    # empty out the tree and vertices
    self.rrt_tree = MultiPoint()
    self.rrt_vertices.clear()
    self.rrt_edges.clear()
    # add root point to tree
    self.add_start_vertex(root)

    final_pt = None
    final_pt_cost = float('inf')

    # iterate until max iterations is reached or goal is reached
    i = 0
    while i < self.iters or final_pt is None:
      if self.display_every_n >= 1 and i % (self.display_every_n*2) == 0:
        print(f"RRT building iteration {i}")
        # if self.gui and i > 1000 and i % 1000 == 0:
        #   self.render(visualize=True)

      # sample a node, find the nearest existing node, and steer from nearest to sampled
      x_rand = self.sample_free(goal_pt, buffer_radius=0 if i > self.iters/2 and final_pt is None else self.obstacle_avoidance_radius)
      x_nearest = self.find_nearest(x_rand)
      x_new = self.steer(x_nearest, x_rand)

      # if there is an obstacle free path from the nearest node to the new node, analyze neighbors and add to tree
      if self.edge_obstacle_free(x_nearest, x_new):
        # find nearby points to the new point
        nearby_points = self.find_nearby_pts(x_new)

        # get the minimum point from the set
        x_min,min_cost = self.get_min_cost_point(nearby_points, x_nearest, x_new)

        # add the new point to the tree
        self.add_vertex(pt=x_new,parent=x_min,cost=min_cost)

        # Main difference between RRT and RRT*, modify the points in the nearest set to optimise local path costs.
        self.do_rrtstar_rewiring(nearby_points, x_min, x_new)

        # check if we've reached the goal of the tree building
        if self.reached_goal(x_new, goal=goal_pt, threshold=goal_threshold):
          if self.built_tree: # subsequent runs should just terminate once goal is reached
            final_pt = x_new
            break
          else: # keep searching and update the shortest path
            if min_cost < final_pt_cost:
              final_pt = x_new
              final_pt_cost = min_cost
      i += 1
    return final_pt,final_pt_cost

  def extract_path(self, *, endpoint: Point, root: Point, reverse:bool = True) -> list[Node]:
    """
    Extracts the path from the root of rrt tree to the endpoint
    Done by starting from endpoint in tree and iterating over parents until root is reached
    """
    curr = endpoint
    path: list[Point] = []
    while curr != root:
      if curr is None:
        print("ERROR: curr is None!", list(map(str,path)))
        self.render(visualize=True)
        break
      if reverse: # get parent before adding (such that final path will include root but not endpoint)
        curr = self.get_parent(curr)
        path.append(curr)
      else: # add point before getting parent (such that final path will include endpoint but not root)
        path.append(curr)
        curr = self.get_parent(curr)
        
    if not reverse: # invert condition because path is already reversed since iterating backwards via parents
      path.reverse() # curr pos first
    
    # convert to nodes with parent relationships
    node_path = []
    for i,pt in enumerate(path):
      parent = self.curr_pos if i == 0 else node_path[i-1]
      node_path.append(Node(pt,parent))
    return node_path

  def get_parent(self, point: Point, /,*, allow_none:bool = False) -> Point:
    if pt2tuple(point) not in self.child_to_parent_map: # happens during severing for farrtstar
      if allow_none:
        return None
      else:
        print("ERROR: parent is None!", point)
        self.render(visualize=True)
        raise ValueError(f"No parent found for point {point}")
    parent = self.child_to_parent_map[pt2tuple(point)]
    return Point(parent)

  def get_children(self, point: Point, /) -> set[vertex_t]:
    return self.parent_to_children_map[pt2tuple(point)]

  def set_child_parent(self, /,*, child: Point, parent: Point) -> None:
    """
    Sets the parent of a child node
    Breaks any existing relationships between child and a previous parent if present
    """
    # convert child to hashable type
    child = pt2tuple(child)

    # check if the child already has a parent - sever the connection
    old_parent = self.get_parent(child, allow_none=True)
    if old_parent is not None:
      self.get_children(old_parent).discard(child)

    # if the new parent is None, remove the child from the children map
    if parent is None and child in self.parent_to_children_map:
      del self.child_to_parent_map[child]
      return
    
    # convert parent to hashable type
    parent = pt2tuple(parent)
    # set the new parent and add the child to the children map
    self.child_to_parent_map[child] = parent
    self.parent_to_children_map[parent].add(child)

  def get_cost_to_reach(self, point: Point) -> float:
    return self.cost_to_reach[pt2tuple(point)]

  def set_cost_to_reach(self, point: Point, cost: float) -> None:
    self.cost_to_reach[pt2tuple(point)] = cost

  def get_edge_cost(self, point1: Point, point2: Point) -> float:
    return point1.distance(point2)

  def get_render_kwargs(self) -> dict:
    return {
      'rrt_tree': self.rrt_tree,
      'rrt_parents': self.child_to_parent_map
    }


if __name__=='__main__':
  world = World()
  rrt_star = RRTStar(world=world, x_start=Node(world.random_position(not_blocked=True)), x_goal=Node(world.random_position(not_blocked=True)), gui=True)
  rrt_star.run()