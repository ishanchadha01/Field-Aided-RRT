import argparse
import math
from queue import PriorityQueue, Queue
import queue
import random
from matplotlib import pyplot as plt
import numpy as np
from shapely.geometry import Point, LineString, MultiPoint, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points

from scipy.ndimage import gaussian_filter

from farrt.node import Node
from farrt.plot import plot_point, plot_potential_field, plot_world
from farrt.rrtstar import RRTStar
from farrt.runner import main
from farrt.world import World
from farrt.utils import as_multipoint, as_multipolygon, as_point, multipoint_without, pt2tuple, shapely_edge


vertex_t = tuple[float,float]
edge_t = tuple[vertex_t,vertex_t]

class FARRTStar2(RRTStar):

  def __init__(self, *args, **kwargs) -> None:
    self.merge_threshold = kwargs.pop('merge_threshold', None)
    self.potential_field_force = kwargs.pop('potential_field_force', 3)
    self.tree_attr_force = kwargs.pop('tree_attr_force', 0.5)
    self.goal_attr_force = kwargs.pop('goal_attr_force', 0.2)
    super().__init__(*args, **kwargs)
    if self.merge_threshold is None:
      self.merge_threshold = self.steer_distance / 8

    self.iters = max(self.iters, 5000)

    # self.free_points: MultiPoint = MultiPoint()
    self.potential_field = np.zeros((*self.world.dims, len(self.world.dims)))

    self.rewiringPQ: PriorityQueue = PriorityQueue()
    self.queue_key_map: dict[vertex_t, float] = dict()

  def setup_planner(self) -> None:
    if not self.detected_obstacles.is_empty:
      self.update_potential_field(self.detected_obstacles)
    super().setup_planner()

  def replan(self, new_obstacles: MultiPolygon, **kwargs):
    """
    Replan the path from the current position to the goal.
    1. Sever nodes within obstacles
    2. Apply potential field update to remaining points
    3. Rewire free points into tree
    4. Extract a plan from the tree
    """
    if self.gui:
      # print('Render observations...')
      self.render(draw_world_obstacles=False, save_frame=True, **kwargs)

    # clear the previous plan
    previous_plan = self.planned_path
    self.planned_path = []

    # sever nodes within obstacles and get the descendants as free points
    # internally adds freed nodes to the rewiring queue
    removed_pts, closest_parents, freed_pts = self.do_tree_severing(previous_plan)
    print(f'removed: {len(removed_pts.geoms)}')
    print(f'closest: {len(closest_parents.geoms)}')
    print(f'freed: {len(freed_pts.geoms)}')

    # update potential field based on new obstacles
    self.update_potential_field(new_obstacles)

    # # push free points with the potential field
    # #   different from proposal - only field updating for free points (not whole tree)
    # pushed_pts = self.apply_potential_field(self.free_points.union(freed_pts))
    # # merge very close points into one
    # pushed_pts = self.merge_points(pushed_pts)

    # set internal freed points to the field-updated free points
    # self.free_points = multipoint_without(freed_pts, self.detected_obstacles)

    # rewire the free points into the tree until curr pos is reached
    final_pt,final_cost = self.do_farrt_rewiring(goal=self.curr_pos.coord, initial_frontier=closest_parents)
    
    # extract a plan from the tree and reverse it (to go from goal to start)
    self.planned_path = self.extract_path(endpoint=final_pt,root=self.x_goal_pt,reverse=True)

    print('Planning complete')
    # if self.gui:
    #   self.render()

  def sever_connections(self, pt: vertex_t):
    """
    Remove point from tree and cut all edges to/from the point
    Makes `pt` a free point
    """
    vtx = pt2tuple(pt)

    parent = self.get_parent(pt, allow_none=True)
    if parent is not None:
      parent_vtx = pt2tuple(parent)
      self.rrt_edges.discard((vtx, parent_vtx))
      self.rrt_edges.discard((parent_vtx, vtx))
      self.set_child_parent(child=pt, parent=None)

    children = list(self.get_children(pt))
    for other in children:
      self.rrt_edges.discard((vtx, other))
      self.rrt_edges.discard((other, vtx))
      self.set_child_parent(child=other, parent=None)
    
    self.rrt_vertices.discard(vtx)
    self.rrt_tree = multipoint_without(self.rrt_tree, as_point(pt))

    self.set_cost_to_goal(pt, float('inf'))

  def do_tree_severing(self, previous_plan: list[Node]) -> tuple[MultiPoint, MultiPoint, MultiPoint, PriorityQueue]:
    """
    Sever nodes within obstacles and get the descendants as free points
    Also get the most recent parents to the deleted nodes
    """
    print('Severing tree...')
    # get the points that are within the obstacles (or within half the obstacle avoidance radius of them)
    conflicting_pts = as_multipoint(self.rrt_tree.intersection(self.detected_obstacles.buffer(self.obstacle_avoidance_radius/2)))
    # add points that have an edge which is not obstacle free
    for node in previous_plan:
      if node.parent is not None and node.edgeToParent().intersects(self.detected_obstacles):
        conflicting_pts = conflicting_pts.union(node.parent.coord)

    # determine the closest parents of the conflicting points - defaults to boundary zone around removed points
    closest_parents: MultiPoint = multipoint_without(self.rrt_tree.intersection(self.detected_obstacles.buffer(self.obstacle_avoidance_radius)), conflicting_pts)
    # build a Q for freeing all descendants of conflicting points
    severed_children_q: Queue[vertex_t] = Queue()
    for pt in conflicting_pts.geoms:
      closest_parents = closest_parents.union(self.get_parent(pt))
      severed_children_q.put(pt2tuple(pt))

    # if self.gui:
    #   print('Severing tree... - conflicting points in white')
    #   self.render(visualize=True, save_frame=True, extra_points=conflicting_pts)

    # ensure that none of the closest parents are in the conflicting points
    closest_parents = closest_parents - conflicting_pts

    # use Q to get all the descendants of the conflicting points
    severed_children: set[vertex_t] = set()
    while not severed_children_q.empty():
      point = severed_children_q.get()
      severed_children.add(point) # add pt to set
      children = self.get_children(point) # get children of pt
      new_children = children - severed_children # shouldnt be necessary
      for child in new_children: # add children to Q
        severed_children_q.put(child)
    
    # if self.gui:
    #   print('find children to sever - nodes to be severed in white')
    #   self.render(visualize=True, save_frame=True, extra_points=as_multipoint(severed_children))

    # actually sever all children from the tree/vertex sets
    for child in severed_children:
      self.sever_connections(child)

    # add the orphans to the rewiring queue
    for child in severed_children:
      if not self.point_obstacle_free(child):
        continue
      self.insertPQ(child)

    # make only points actually in obstacles get deleted, the rest can be freed
    conflicting_pts = as_multipoint(conflicting_pts.intersection(self.detected_obstacles))

    # the freed points are all the severed nodes except conflicting points (conflicting points are deletec)
    freed_pts = multipoint_without(severed_children, conflicting_pts)

    # ensure that none of the closest parents are in the freed points
    closest_parents = as_multipoint(closest_parents - freed_pts)

    # render the tree with the severing results
    if self.gui:
      # print('Tree severing :: dark->light = closest parents -> conflicting -> freed')
      self.render(save_frame=True, extra_points={
        'moccasin': freed_pts,
        'darkorange': conflicting_pts,
        'peru': closest_parents,
      })

    return conflicting_pts, closest_parents, freed_pts

  def update_potential_field(self, new_obstacles: MultiPolygon, /):
    """
    Update the potential field based on new obstacles
    Convert the obstacle into a binary mask as a numpy array
    blur the mask and take te gradient of the blurred mask to get directions of force for the field update
    """
    print('Updating potential field...')
    obstacle_value = 5
    blur_sigma = 3

    # update potential field based on new obstacles
    obstacle_mask = np.ones(tuple(self.world.dims))

    # get a mask of all coordinates contained within the new obstacles
    obstacle: Polygon
    for obstacle in new_obstacles.geoms:
      minx, miny, maxx, maxy = obstacle.bounds
      for x in range(math.floor(minx), math.ceil(maxx)+1):
        for y in range(math.floor(miny), math.ceil(maxy)+1):
          if obstacle.contains(Point(x, y)):
            obstacle_mask[y,x] = 0
    
    # blur the mask
    blurred_mask = np.array(gaussian_filter(obstacle_mask*obstacle_value, sigma=blur_sigma), dtype=float)

    # take the gradient of the blurred mask
    dy,dx = np.gradient(blurred_mask)

    self.potential_field[:,:,0] += dx
    self.potential_field[:,:,1] += dy

    if self.gui:
      # print(f'Potential field updated with new obstacle')
      self.render(save_frame=True, potential_field=self.potential_field)
  
  def merge_points(self, points: MultiPoint, merge_threshold: float = None) -> MultiPoint:
    """
    merge points that are very close together
    """
    print('Merging points...')
    if merge_threshold is None:
      merge_threshold = self.merge_threshold
    merged_pts: list[Point] = []

    # buffer all the points by half the merge threshold (create a collection of circles centered at each point)
    buffered = as_multipolygon(points.buffer(merge_threshold / 2))

    # take the centroid of every resulting polygon in the buffered shape
    #   points within the merge threshold will have merged circles, so they will only result in a single larger polygon in the buffered shape
    for poly in buffered.geoms:
      merged_pts.append(poly.centroid)
    
    print(f'Merged {len(points.geoms)} points into {len(merged_pts)} points')
    if self.gui:
      # print(f'After merging - points:white  merged:cyan')
      self.render(save_frame=True, extra_points={
        'white': points,
        'cyan': merged_pts,
      })

    # convert the list of points to a multipoint
    return as_multipoint(merged_pts)

  def apply_field_to_pt(self, pt: Point, /,*,field_force:float=None,tree_force:float = None,goal_force:float=None) -> Point:
    """
    push the given free points based on the potential field from the obstacles
    """
    if field_force is None:
      field_force = self.potential_field_force * self.explored_region.area / self.detected_obstacles.area
    if tree_force is None:
      tree_force = self.tree_attr_force
    if goal_force is None:
      goal_force = self.goal_attr_force
    xInd = min(max(0, math.floor(pt.x)), self.world.dims[0]-1)
    yInd = min(max(0, math.floor(pt.y)), self.world.dims[1]-1)
    dx,dy = self.potential_field[yInd,xInd] * field_force
    nearby_pts = self.find_nearby_pts(pt, radius=self.steer_distance * 1.5, pt_source=self.rrt_tree)
    if not nearby_pts.is_empty:
      avg_tree_point = nearby_pts.centroid
      diff = np.array([avg_tree_point.x - pt.x, avg_tree_point.y - pt.y])
      diff *= tree_force / np.linalg.norm(diff)
      dx += diff[0]
      dy += diff[1]
    goal_diff = np.array([self.x_goal_pt.x - pt.x, self.x_goal_pt.y - pt.y])
    goal_diff *= goal_force / np.linalg.norm(goal_diff)
    dx += goal_diff[0]
    dy += goal_diff[1]
    # print(f'Field effect: {dx,dy} - mag={math.sqrt(dx**2 + dy**2)}')
    newX = min(max(0, pt.x + dx), self.world.dims[0])
    newY = min(max(0, pt.y + dy), self.world.dims[1])
    
    return Point(newX, newY)

  def do_farrt_rewiring(self, /,*, goal:Point, initial_frontier: MultiPoint):
    """
    Rewire the free points into the tree
    Sample from self.free_points based on the passed in initial_frontier (closest_parents)
      (the frontier of nodes which were severed)
    """
    print('Rewiring free points...')
    if self.gui:
      print(f'Start rewiring :: thistle - initial fronter')
      self.render(save_frame=True, extra_points={
        'thistle': initial_frontier
      })

    curr_vtx = pt2tuple(self.curr_pos)
    final_pt = None
    final_pt_cost = float('inf')
    
    # frontier = initial_frontier

    def testQtop(v_bot):
      """
      Test the top of the inconsistencyPQ against v_bot
      line 1 [keyLess] from Algo5: reduceInconsistency() from RRTx paper
      """
      top = self.popPQ()
      key_less = self.test_key_less(top, v_bot)
      self.insertPQ(top)
      # if key_less:
      #   print(f'{top=} has key less {v_bot=}')
      # else:
      #   print(f'{top=} has key greater {v_bot=}')
      return key_less

    i = 0
    while (self.queue_not_empty()\
      and (False
        or self.get_cost_to_goal(self.curr_pos) == float('inf') # g(v_bot) = inf
        or pt2tuple(self.curr_pos) in self.queue_key_map
        or testQtop(pt2tuple(self.curr_pos))
      )) or final_pt is None:
      if self.display_every_n >= 1 and i % (self.display_every_n*2) == 0:
        print(f"FARRT rewiring iteration {i}")
        if self.gui and i > 400 and i % 100 == 0:
          print(f"FARRT rewiring iteration {i} - frontier in white")
          self.render(visualize=True)#, extra_points=frontier)

      # sample a node from free points, find the nearest existing node, and steer from nearest to sampled
      # x_free = self.sample_free_pts(frontier, goal_pt=goal)
      x_free = as_point(self.popPQ()) if self.queue_not_empty() else self.sample_free(goal, buffer_radius=0)
      if x_free != self.curr_pos.coord: # apply the field if not trying to rewire self
        x_field = self.apply_field_to_pt(x_free)
      else:
        x_field = x_free
      x_nearest = self.find_nearest(x_field, pt_source=self.rrt_tree)
      x_new = self.steer(x_nearest, x_field)
      # if x_new != x_field: # if the new point is not the same as the sampled point
      #   print(f"Steering from {x_nearest} to {x_field} to get {x_new} - frontier in white")
      #   allow_for_far_goal = x_field == goal and not goal.buffer(self.steer_distance).intersects(self.free_points)
      #   if i > 99:
      #     if allow_for_far_goal:
      #       print('failed to steer to goal, must be too far! - allow new node creation')
      #     # self.render(visualize=True, extra_points=frontier)
      #   if not allow_for_far_goal:
      #     continue

      # if there is an obstacle free path from the nearest node to the new node, analyze neighbors and add to tree
      if self.edge_obstacle_free(x_nearest, x_new):
        # find nearby points to the new point
        nearby_points = self.find_nearby_pts(x_new, radius=self.find_ball_radius(num_vertices=len(self.rrt_vertices)), pt_source=self.rrt_tree)

        # get the minimum point from the set
        x_min,min_cost = self.get_min_cost_point(nearby_points, x_nearest, x_new)

        # add the new point to the tree
        self.add_vertex(pt=x_new,parent=x_min,cost=min_cost)
        self.free_points = self.free_points - x_new
        # frontier = (frontier - x_min).union(x_new)

        # Main difference between RRT and RRT*, modify the points in the nearest set to optimise local path costs.
        self.do_rrtstar_rewiring(nearby_points, x_min, x_new)

        for orphan in multipoint_without(nearby_points, self.rrt_tree).geoms:
          self.verify_queue(orphan)

        # check if we've reached the goal of the tree building
        if self.reached_goal(x_new, goal=goal, threshold=0):
          if self.built_tree: # subsequent runs should just terminate once goal is reached
            final_pt = x_new
            break
          else: # keep searching and update the shortest path
            if min_cost < final_pt_cost:
              final_pt = x_new
              final_pt_cost = min_cost
      # else: # steer failed -> reinsert node for rewiring
      #   print(f'Obstacle blocking from nearest={pt2tuple(x_nearest)} to field_pushed={pt2tuple(x_field)} to get new={pt2tuple(x_new)}')
      #   self.insertPQ(x_field)
      i += 1

    return final_pt,final_pt_cost
  
  
  def verify_queue(self, pt: Point, /) -> None:
    """
    Adds v to the queue if it is not already in the queue
    """
    pt = pt2tuple(pt)
    if pt in self.queue_key_map:
      self.updatePQ(pt)
    else:
      self.insertPQ(pt)

  def get_key(self, pt: Point) -> tuple[float,float]:
    """
    Return the key of pt
    """
    nearest = self.find_nearest(as_point(pt), pt_source=self.rrt_tree)
    cost = self.get_cost_to_goal(nearest) + self.get_edge_cost(nearest, pt)
    return (cost)

  def test_key_less(self, v_1, v_2) -> bool:
    """
    Test if v_1 is less than v_2 in the key ordering
    keyLess for Algo5: reduceInconsistency() from RRTx paper
    """
    return self.get_key(v_1) < self.get_key(v_2)

  def insertPQ(self, pt: Point) -> None:
    """
    Insert v into the inconsistencyPQ
    """
    pt = pt2tuple(pt)
    key = self.get_key(pt)
    # only add it if not already present
    if pt not in self.queue_key_map:
      self.queue_key_map[pt] = key
      self.rewiringPQ.put((key,pt))
    else:
      print(f'Warning: {pt} already in inconsistencyPQ - {self.queue_key_map[pt]}')

  def updatePQ(self, pt: Point) -> None:
    """
    Update the key of v in the inconsistencyPQ
    """
    pt = pt2tuple(pt)
    key = self.get_key(pt)
    # only update it if it is already present
    if pt in self.queue_key_map:
      self.queue_key_map[pt] = key
      self.rewiringPQ.put((key,pt))
    else:
      print(f'Warning: {pt} not in inconsistencyPQ, tried to update - {key}')

  def popPQ(self) -> Point:
    """
    Pop the top of the inconsistencyPQ
    """
    key,point = self.rewiringPQ.get(block=False)
    # ensure that the point is still in the inconsistent set
    while point not in self.queue_key_map or self.queue_key_map[point] != key:
      point = self.rewiringPQ.get(block=False)
    self.queue_key_map.pop(point)
    return point

  def queue_not_empty(self) -> bool:
    """
    Return whether the inconsistencyPQ is not empty
    """
    try:
      # key,point = self.inconsistencyPQ.get(block=False)
      # self.inconsistencyPQ.put((key,point))
      point = self.popPQ()
      self.insertPQ(point)
      return True
    except queue.Empty:
      return False

  def get_render_kwargs(self) -> dict:
    return {
      **super().get_render_kwargs(),
      'free_points': as_multipoint(list(self.queue_key_map.keys())),
      'rrt_children': self.parent_to_children_map,
    }

MAP_WITH_TINY_BOTTOM_GAP = "MULTIPOLYGON (((77.98018699847196 48.890924263977666, 77.98018699847196 56.337998124260366, 80.61025492734979 56.337998124260366, 80.61025492734979 58.641893620692386, 78.94620764379944 58.641893620692386, 78.94620764379944 65.13100088335415, 84.23211592196931 65.13100088335415, 84.23211592196931 65.41640495500974, 85.49277807536407 65.41640495500974, 85.49277807536407 69.21990912844198, 89.07054947476675 69.21990912844198, 89.07054947476675 75.78971021239619, 87.97343013294632 75.78971021239619, 87.97343013294632 80.80840275593098, 92.99212267648112 80.80840275593098, 92.99212267648112 78.51935985942984, 98.75530816274625 78.51935985942984, 98.75530816274625 68.83460117145034, 90.23602159286852 68.83460117145034, 90.23602159286852 65.41640495500974, 91.1047148720486 65.41640495500974, 91.1047148720486 58.543806004930445, 85.96307644591067 58.543806004930445, 85.96307644591067 56.337998124260366, 89.542261550403 56.337998124260366, 89.542261550403 44.77592357232933, 79.00748548659598 44.77592357232933, 79.00748548659598 43.79751712329747, 81.06040908557229 43.79751712329747, 81.06040908557229 39.923610881731996, 85.75502306204302 39.923610881731996, 85.75502306204302 30.611357644000343, 77.73168816269443 30.611357644000343, 77.73168816269443 19.956937688670685, 66.8946208282358 19.956937688670685, 66.8946208282358 30.7940050231293, 76.44276982431137 30.7940050231293, 76.44276982431137 31.883561579635707, 76.36737925042237 31.883561579635707, 76.36737925042237 32.158816613843925, 73.13863861732922 32.158816613843925, 73.13863861732922 30.954166160495994, 67.60283136259899 30.954166160495994, 67.60283136259899 36.489973415226224, 72.7003665703187 36.489973415226224, 72.7003665703187 37.15134244926887, 73.16187992565399 37.15134244926887, 73.16187992565399 38.569179014087325, 68.68574023670564 38.569179014087325, 68.68574023670564 44.79626264391389, 64.8645604339525 44.79626264391389, 64.8645604339525 36.71773020520898, 53.158746581519324 36.71773020520898, 53.158746581519324 48.42354405764216, 53.234200092587315 48.42354405764216, 53.234200092587315 48.89832854068845, 44.09566638516624 48.89832854068845, 44.09566638516624 58.802388398784444, 53.99972624326222 58.802388398784444, 53.99972624326222 56.235609102689885, 61.20080990018164 56.235609102689885, 61.20080990018164 59.24767893377622, 65.03283046182534 59.24767893377622, 65.03283046182534 64.68725520759932, 66.26260695420987 64.68725520759932, 66.26260695420987 68.50401738475666, 77.34495277797257 68.50401738475666, 77.34495277797257 57.42167156099398, 75.71038347601407 57.42167156099398, 75.71038347601407 54.00970219341058, 71.63248435258103 54.00970219341058, 71.63248435258103 48.890924263977666, 77.98018699847196 48.890924263977666)), ((49.468689827608216 16.20126003346217, 49.468689827608216 6.076010693218409, 38.94600450681244 6.076010693218409, 38.94600450681244 16.598696014014187, 44.41006743211651 16.598696014014187, 44.41006743211651 23.476537761716457, 46.50038793408816 23.476537761716457, 46.50038793408816 28.033956015672636, 58.33308391629863 28.033956015672636, 58.33308391629863 16.20126003346217, 49.468689827608216 16.20126003346217)), ((0 93.35673979516416, 0 99.44072745811725, 4.868661380675365 99.44072745811725, 4.868661380675365 93.35673979516416, 0 93.35673979516416)), ((23.050971176307705 83.8210173178498, 20.81824335292373 83.8210173178498, 20.81824335292373 85.34862264261028, 20.37312788752331 85.34862264261028, 20.37312788752331 82.52253131553579, 14.213803093458798 82.52253131553579, 14.213803093458798 88.68185610960029, 17.363505375471107 88.68185610960029, 17.363505375471107 94.82931376233627, 15.76120148735365 94.82931376233627, 15.76120148735365 99.05776118027215, 19.989648905289535 99.05776118027215, 19.989648905289535 95.88032723712578, 27.895209969986624 95.88032723712578, 27.895209969986624 91.29311747325208, 33.417519562720535 91.29311747325208, 33.417519562720535 86.47908433885367, 35.99711481882477 86.47908433885367, 35.99711481882477 89.94387204179279, 43.89164499561646 89.94387204179279, 43.89164499561646 82.04934186500108, 37.62795275889709 82.04934186500108, 37.62795275889709 78.28978396908145, 29.43865238912489 78.28978396908145, 29.43865238912489 79.00155355017372, 25.631126017030716 79.00155355017372, 25.631126017030716 80.92656908683925, 23.050971176307705 80.92656908683925, 23.050971176307705 83.8210173178498)), ((84.6141615220117 92.43403031703808, 85.47342296494851 92.43403031703808, 85.47342296494851 95.71658138100312, 86.7182173180298 95.71658138100312, 86.7182173180298 100, 98.31523052881347 100, 98.31523052881347 94.56901328395648, 99.08666300661923 94.56901328395648, 99.08666300661923 93.49842248154893, 99.63071650520918 93.49842248154893, 99.63071650520918 84.06106358084536, 99.08666300661923 84.06106358084536, 99.08666300661923 83.45736221994412, 87.97501194260687 83.45736221994412, 87.97501194260687 85.51155454416917, 85.47342296494851 85.51155454416917, 85.47342296494851 87.80150251992295, 85.12231354716515 87.80150251992295, 85.12231354716515 85.7029696803328, 79.13201773915641 85.7029696803328, 79.13201773915641 91.69326548834154, 84.6141615220117 91.69326548834154, 84.6141615220117 92.43403031703808)), ((0.5594967329337708 29.054290938218347, 0.5594967329337708 38.19659020359191, 9.15922519274988 38.19659020359191, 9.15922519274988 38.31613567781571, 8.02247313107741 38.31613567781571, 8.02247313107741 48.0947662599947, 9.15922519274988 48.0947662599947, 9.15922519274988 48.15400973228169, 14.017026417357595 48.15400973228169, 14.017026417357595 55.852311347855284, 24.625159098232427 55.852311347855284, 24.625159098232427 45.24417866698045, 19.459391796847566 45.24417866698045, 19.459391796847566 37.85384312818401, 9.701795998307329 37.85384312818401, 9.701795998307329 29.054290938218347, 0.5594967329337708 29.054290938218347)), ((53.64836480985205 76.31077036378733, 53.64836480985205 84.00731592700986, 64.8132619646621 84.00731592700986, 64.8132619646621 72.84241877219979, 64.65808202031778 72.84241877219979, 64.65808202031778 71.84744381595281, 55.99798171579497 71.84744381595281, 55.99798171579497 72.84241877219979, 55.880801461468806 72.84241877219979, 55.880801461468806 71.10164597942361, 59.578593637490314 71.10164597942361, 59.578593637490314 61.83825123102174, 50.31519888908845 61.83825123102174, 50.31519888908845 67.1596577339813, 46.729688831662784 67.1596577339813, 46.729688831662784 76.31077036378733, 53.64836480985205 76.31077036378733)), ((77.9279216968029 7.381514894420175, 77.9279216968029 18.873130809469405, 89.41953761185214 18.873130809469405, 89.41953761185214 17.829316493748827, 97.59040508484543 17.829316493748827, 97.59040508484543 6.667790517692625, 86.42887910878923 6.667790517692625, 86.42887910878923 7.381514894420175, 77.9279216968029 7.381514894420175)), ((92.77443931214958 28.848021295846266, 92.77443931214958 39.40072498427401, 100 39.40072498427401, 100 28.848021295846266, 92.77443931214958 28.848021295846266)), ((24.491922362863008 2.150517903149498, 24.491922362863008 12.977835747640139, 35.31924020735365 12.977835747640139, 35.31924020735365 11.482733200255279, 36.77963350560457 11.482733200255279, 36.77963350560457 0.5963906980664078, 25.893291003415698 0.5963906980664078, 25.893291003415698 2.150517903149498, 24.491922362863008 2.150517903149498)), ((51.82327121708119 90.6546107359112, 44.29437834916575 90.6546107359112, 44.29437834916575 98.18350360382664, 44.56550658331211 98.18350360382664, 44.56550658331211 100, 53.123484341776766 100, 53.123484341776766 95.30900319780739, 51.82327121708119 95.30900319780739, 51.82327121708119 90.6546107359112)), ((5.088782628438379 84.77169683662218, 5.088782628438379 77.29650492864094, 0 77.29650492864094, 0 84.77169683662218, 5.088782628438379 84.77169683662218)), ((28.12557996533808 69.79138201369098, 29.580815044031333 69.79138201369098, 29.580815044031333 61.78437506007718, 21.573808090417543 61.78437506007718, 21.573808090417543 69.79138201369098, 22.039458211249116 69.79138201369098, 22.039458211249116 70.88491851599834, 28.12557996533808 70.88491851599834, 28.12557996533808 69.79138201369098)), ((2.9350022296922926 60.050214278838006, 2.9350022296922926 70.53414505104129, 13.418933001895587 70.53414505104129, 13.418933001895587 60.050214278838006, 2.9350022296922926 60.050214278838006)), ((11.778623522656304 90.16657554972596, 11.778623522656304 84.58306727657347, 6.195115249503804 84.58306727657347, 6.195115249503804 90.16657554972596, 11.778623522656304 90.16657554972596)), ((41.4576812467833 25.077094186359055, 31.216577461799467 25.077094186359055, 31.216577461799467 35.31819797134288, 37.86520301887402 35.31819797134288, 37.86520301887402 42.17660558545712, 41.05671556770504 42.17660558545712, 41.05671556770504 44.51778495829461, 42.41981471079389 44.51778495829461, 42.41981471079389 48.07782848466981, 50.36245016191344 48.07782848466981, 50.36245016191344 44.51778495829461, 52.87794586955499 44.51778495829461, 52.87794586955499 32.696554656444675, 41.4576812467833 32.696554656444675, 41.4576812467833 25.077094186359055)), ((78.61600283663337 92.91030381771562, 78.61600283663337 82.42016577759587, 68.12586479651362 82.42016577759587, 68.12586479651362 92.91030381771562, 78.61600283663337 92.91030381771562)), ((62.66137287084606 10.754432869467118, 57.33125317412006 10.754432869467118, 57.33125317412006 16.084552566193118, 62.66137287084606 16.084552566193118, 62.66137287084606 10.754432869467118)), ((28.908898631433907 53.17774585000669, 35.326024950546646 53.17774585000669, 35.326024950546646 46.76061953089395, 28.908898631433907 46.76061953089395, 28.908898631433907 53.17774585000669)), ((96.1445894202975 26.61466513779553, 100 26.61466513779553, 100 20.496998712227526, 96.1445894202975 20.496998712227526, 96.1445894202975 26.61466513779553)), ((3.5634931654748225 76.42562833300869, 3.5634931654748225 71.24515450227531, 0 71.24515450227531, 0 76.42562833300869, 3.5634931654748225 76.42562833300869)))"
# start = Point(9.385958677423488, 2.834747652200631)
# goal = Point(76.2280082457942, 0.2106053351110693)

MAP_WITH_PASSAGE = "MULTIPOLYGON (((20 75, 45 75, 50 75, 50 70, 50 30, 50 25, 45 25, 20 25, 20 30, 45 30, 45 70, 20 70, 20 75)), ((70 100, 80 100, 80 55, 70 55, 70 100)), ((80 45, 80 0, 70 0, 70 45, 80 45)))"
# start = Point(40, 50)
# goal = Point(90, 50)

MAP_CLUTTER = "MULTIPOLYGON (((23.064770251108737 22.68576118147672, 21.322484507375187 22.68576118147672, 21.322484507375187 21.33606103177675, 19.697172798879073 21.33606103177675, 19.697172798879073 15.641970295079886, 11.040869760762426 15.641970295079886, 11.040869760762426 24.298273333196533, 13.556625230683117 24.298273333196533, 13.556625230683117 29.10192030846882, 18.356531732787502 29.10192030846882, 18.356531732787502 33.445307633143386, 29.116078184454167 33.445307633143386, 29.116078184454167 23.894981658989355, 34.885904691841205 23.894981658989355, 34.885904691841205 23.167620176967606, 35.03638106773981 23.167620176967606, 35.03638106773981 11.196009360336536, 23.064770251108737 11.196009360336536, 23.064770251108737 22.68576118147672)), ((63.17114914812499 43.99286423808687, 58.98119479151418 43.99286423808687, 58.98119479151418 48.18281859469768, 62.10777488481454 48.18281859469768, 62.10777488481454 51.223212184537715, 57.31727245100156 51.223212184537715, 57.31727245100156 49.02622523055536, 47.52476316077278 49.02622523055536, 47.52476316077278 47.37830016015137, 42.0865627954879 47.37830016015137, 42.0865627954879 40.12408653055279, 34.299249996009955 40.12408653055279, 34.299249996009955 47.91139933003073, 40.867369731681904 47.91139933003073, 40.867369731681904 54.03569358924224, 43.51985505579762 54.03569358924224, 43.51985505579762 60.09556775389259, 46.23737826194491 60.09556775389259, 46.23737826194491 60.10611941961201, 48.3426172590429 60.10611941961201, 48.3426172590429 65.76486736748672, 49.190228473662316 65.76486736748672, 49.190228473662316 68.16449541971436, 54.58483543284498 68.16449541971436, 54.58483543284498 77.58414360403232, 58.16248777303599 77.58414360403232, 58.16248777303599 81.79641056548523, 66.05065261928203 81.79641056548523, 66.05065261928203 86.14506487114295, 77.76215303054846 86.14506487114295, 77.76215303054846 74.43356445987652, 74.76123973455 74.43356445987652, 74.76123973455 73.07085200606188, 78.47713414222854 73.07085200606188, 78.47713414222854 63.76330563227871, 76.77642313431451 63.76330563227871, 76.77642313431451 58.40336819795139, 67.58506037447664 58.40336819795139, 67.58506037447664 56.38376059611299, 72.83726546415194 56.38376059611299, 72.83726546415194 45.6542700167756, 63.17114914812499 45.6542700167756, 63.17114914812499 43.99286423808687), (65.58240279488808 58.84373588967048, 65.58240279488808 63.65462043655472, 61.47260906558145 63.65462043655472, 61.47260906558145 67.70401793908896, 61.183550990199464 67.70401793908896, 61.183550990199464 57.5603025794461, 62.16655867210867 57.5603025794461, 62.16655867210867 56.38376059611299, 63.14886323885532 56.38376059611299, 63.14886323885532 58.84373588967048, 65.58240279488808 58.84373588967048), (66.5776918874721 71.90514631681582, 64.46496109778835 71.90514631681582, 64.46496109778835 70.30721180246809, 66.5776918874721 70.30721180246809, 66.5776918874721 71.90514631681582)), ((90.7584764725099 34.03691996402119, 90.7584764725099 43.509912018313464, 100 43.509912018313464, 100 34.03691996402119, 90.7584764725099 34.03691996402119)), ((7.828693978086511 8.279857175911397, 13.254743949213449 8.279857175911397, 13.254743949213449 12.3007738154921, 21.743133829147226 12.3007738154921, 21.743133829147226 9.278111398861938, 23.696214771101324 9.278111398861938, 23.696214771101324 2.36719874502685, 17.848910257717918 2.36719874502685, 17.848910257717918 0, 7.828693978086511 0, 7.828693978086511 8.279857175911397)), ((25.81984182221841 91.38120410267332, 32.10517265696257 91.38120410267332, 32.10517265696257 79.76504790953756, 20.489016463826793 79.76504790953756, 20.489016463826793 88.14543029627713, 15.75193136287685 88.14543029627713, 15.75193136287685 88.49841352405093, 9.375826337156141 88.49841352405093, 9.375826337156141 95.31037735581117, 15.75193136287685 95.31037735581117, 15.75193136287685 98.21334075561869, 25.81984182221841 98.21334075561869, 25.81984182221841 91.38120410267332)), ((98.63210346917484 91.23287127872267, 87.62638913810085 91.23287127872267, 87.62638913810085 100, 98.63210346917484 100, 98.63210346917484 91.23287127872267)), ((39.885190634159365 35.1522977038896, 39.885190634159365 33.80183748566661, 40.5575027504072 33.80183748566661, 40.5575027504072 28.2872782708653, 39.885190634159365 28.2872782708653, 39.885190634159365 27.067426103347344, 31.800319033617107 27.067426103347344, 31.800319033617107 35.1522977038896, 39.885190634159365 35.1522977038896)), ((4.742896124595053 61.699076287849586, 4.742896124595053 53.960139856393205, 0 53.960139856393205, 0 61.699076287849586, 4.742896124595053 61.699076287849586)), ((64.12800657357118 88.1868182986605, 64.12800657357118 96.62808353931713, 68.44439716214914 96.62808353931713, 68.44439716214914 100, 77.90820858631405 100, 77.90820858631405 91.44447621214137, 72.56927181422782 91.44447621214137, 72.56927181422782 88.1868182986605, 64.12800657357118 88.1868182986605)), ((35.07173681065274 68.22115523321155, 35.07173681065274 64.61612480516, 31.247161885621985 64.61612480516, 31.247161885621985 58.125421177072425, 20.51316214365192 58.125421177072425, 20.51316214365192 52.76298417057445, 18.893103095297406 52.76298417057445, 18.893103095297406 52.678666158350296, 26.802215513340613 52.678666158350296, 26.802215513340613 48.89807570765788, 32.08851735890879 48.89807570765788, 32.08851735890879 47.81359905893655, 33.66529294529682 47.81359905893655, 33.66529294529682 38.955011457959124, 27.72557120791501 38.955011457959124, 27.72557120791501 36.381328419981344, 19.538272927606414 36.381328419981344, 19.538272927606414 42.62543468125382, 16.74898403624414 42.62543468125382, 16.74898403624414 43.87830849945598, 12.754347959021782 43.87830849945598, 12.754347959021782 49.37836711382535, 8.816902340883527 49.37836711382535, 8.816902340883527 56.45490153425605, 9.342696724821538 56.45490153425605, 9.342696724821538 59.346045762563456, 12.656524701650433 59.346045762563456, 12.656524701650433 60.61962161257593, 20.131575953013375 60.61962161257593, 20.131575953013375 69.24100710968104, 29.597747186632642 69.24100710968104, 29.597747186632642 70.0901144291801, 33.69101930945353 70.0901144291801, 33.69101930945353 73.9706414760234, 33.69166300920766 73.9706414760234, 33.69166300920766 82.55380711016599, 43.39955777179233 82.55380711016599, 43.39955777179233 72.84591234758133, 42.26156854798238 72.84591234758133, 42.26156854798238 68.28521971689437, 39.440505552265385 68.28521971689437, 39.440505552265385 68.22115523321155, 35.07173681065274 68.22115523321155)), ((49.16750211506759 27.14104631287844, 41.43961051929601 27.14104631287844, 41.43961051929601 37.335210968460345, 49.54360440981018 37.335210968460345, 49.54360440981018 39.71577058523313, 55.89008622645971 39.71577058523313, 55.89008622645971 36.67533854841258, 60.80740111995378 36.67533854841258, 60.80740111995378 25.03543954352639, 49.16750211506759 25.03543954352639, 49.16750211506759 27.14104631287844)), ((42.83453544702361 94.51270826443918, 42.83453544702361 86.88623274457878, 35.20805992716322 86.88623274457878, 35.20805992716322 94.51270826443918, 42.83453544702361 94.51270826443918)), ((71.8497003258469 3.7121538448466858, 71.8497003258469 8.90446357798789, 77.0420100589881 8.90446357798789, 77.0420100589881 3.7121538448466858, 71.8497003258469 3.7121538448466858)), ((7.245468311300554 32.781995868215176, 7.245468311300554 24.177270806876884, 7.118527980891164 24.177270806876884, 7.118527980891164 20.25211870240142, 0 20.25211870240142, 0 32.781995868215176, 7.245468311300554 32.781995868215176)), ((48.40949784654694 76.49333640706301, 48.40949784654694 81.42140678685193, 53.33756822633586 81.42140678685193, 53.33756822633586 76.49333640706301, 48.40949784654694 76.49333640706301)), ((89.51346949216563 19.49564068455862, 79.18493845235301 19.49564068455862, 79.18493845235301 29.824171724371222, 89.51346949216563 29.824171724371222, 89.51346949216563 19.49564068455862)), ((24.087544483786782 7.16042437161909, 29.45380850114316 7.16042437161909, 29.45380850114316 6.739354162609233, 33.567918856596776 6.739354162609233, 33.567918856596776 1.633691743099965, 28.46225643708751 1.633691743099965, 28.46225643708751 1.7941603542627083, 24.087544483786782 1.7941603542627083, 24.087544483786782 7.16042437161909)), ((92.72129287822813 54.67726122130802, 100 54.67726122130802, 100 45.27863099235755, 93.82470199049146 45.27863099235755, 93.82470199049146 45.419143684832555, 92.72129287822813 45.419143684832555, 92.72129287822813 54.67726122130802)), ((86.16065964141694 86.98563783008571, 78.94197730331587 86.98563783008571, 78.94197730331587 94.20432016818678, 86.16065964141694 94.20432016818678, 86.16065964141694 86.98563783008571)), ((49.36045263463353 82.20070837260732, 43.57623229477778 82.20070837260732, 43.57623229477778 87.98492871246307, 49.36045263463353 87.98492871246307, 49.36045263463353 82.20070837260732)), ((26.08494952879692 77.71187284284085, 26.08494952879692 72.12829833821101, 20.50137502416709 72.12829833821101, 20.50137502416709 77.71187284284085, 26.08494952879692 77.71187284284085)), ((3.6573328138332672 68.32445664732596, 0 68.32445664732596, 0 75.35860819877807, 3.6573328138332672 75.35860819877807, 3.6573328138332672 68.32445664732596)), ((2.708436558131087 95.25107247424806, 0 95.25107247424806, 0 100, 2.708436558131087 100, 2.708436558131087 95.25107247424806)))"
# start = Point(13.436424411240122, 84.74337369372327)
# goal = Point(49.54350870919409, 44.949106478873816)

if __name__=='__main__':
  obs = MAP_CLUTTER
  start = Point(13.436424411240122, 84.74337369372327)
  goal = Point(49.54350870919409, 44.949106478873816)
  main(FARRTStar2, obs, start, goal, gui=False)