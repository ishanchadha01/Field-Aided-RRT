from abc import ABC, abstractmethod
from copy import deepcopy
import os
import shutil
import imageio
from matplotlib import pyplot as plt

from shapely.geometry.base import BaseGeometry
from shapely.geometry import Point

from farrt.node import Node
from farrt.plot import plot_planner
from farrt.world import World

class PartiallyObservablePlanner(ABC):
  def __init__(self, world: World, x_start: Node, x_goal: Node, **kwargs) -> None:
    self.gui = kwargs.pop('gui', True)
    self.outdir = kwargs.pop('outdir', f'{self.__class__.__name__}-gifs')
    self.run_count = kwargs.pop('run_count', PartiallyObservablePlanner.default_run_count(self.outdir))
    self.tmp_img_dir = os.path.join(self.outdir, f'run-{self.run_count}-tmp')
    self.gif_output_path = os.path.join(self.outdir, f'run-{self.run_count}.gif')
    self.display_every_n = kwargs.pop('display_every_n', -1)

    self.world = world
    self.x_start = x_start
    self.x_goal = x_goal
    self.curr_pos = deepcopy(x_start)

    self.vision_radius = kwargs.get('vision_radius', 10)
    self.planned_path: list[Node] = []

    # make sure kwargs is empty
    assert not kwargs, 'Unexpected keyword arguments: {}'.format(kwargs)

  @staticmethod
  def default_run_count(outdir: str) -> int:
    count = 0
    while os.path.exists(os.path.join(outdir, f'run-{count}.gif')):
      count += 1
    return count

  def observe_world(self) -> None:
    """
    update the detected_obstacles geometry based on new observations from the world
    """
    observations = self.world.make_observations(self.curr_pos, self.vision_radius)
    new_obstacles = observations - self.detected_obstacles
    deleted_obstacles = self.detected_obstacles - observations
    self.detected_obstacles = self.detected_obstacles.union(observations)
    if not new_obstacles.is_empty: # new obstacles detected
      self.handle_new_obstacles(new_obstacles)
    if not deleted_obstacles.is_empty: # obstacles disappeared
      self.handle_deleted_obstacles(deleted_obstacles)

  @abstractmethod
  def update_plan() -> None:
    pass

  @abstractmethod
  def handle_new_obstacles(self, new_obstacles: BaseGeometry) -> None:
    """
    called whenever new obstacles are detected at an observation step
    this function is responsible for updating self.planned_path if necessary
    """
    pass

  @abstractmethod
  def handle_deleted_obstacles(self, deleted_obstacles: BaseGeometry) -> None:
    """
    called whenever obstacles are no longer detected at an observation step
    this function is responsible for updating self.planned_path if necessary
    """
    pass

  def step_through_plan(self) -> Node:
    if len(self.planned_path) == 0:
      return self.curr_pos
    return self.planned_path.pop(0)

  def run(self) -> None:
    # setup any gui related stuff before the mainloop
    if self.gui:
      fig,ax = (None,None)#plt.subplots()
      if os.path.exists(self.tmp_img_dir):
        shutil.rmtree(self.tmp_img_dir)
      os.makedirs(self.tmp_img_dir)

    # make initial observation
    self.observe_world()
    step = 0
    while not self.curr_pos.same_as(self.x_goal):
      # render out the planner at the start of each step
      print(f'Step: {step} - Distance: {self.curr_pos.dist(self.x_goal)}')
      if self.gui:
        self.render(save_step=step)

      # follow the planner's current plan and make new observations
      next_node = self.step_through_plan()
      self.curr_pos = next_node
      self.observe_world()
      self.update_plan()

      step += 1

    if self.gui:
      self.render(save_step=step)
      self.save_gif()
      self.delete_tmps()


  @abstractmethod
  def get_render_kwargs(self) -> dict:
    return dict()

  def tmp_img_path(self, step: int, addition: int = 0) -> str:
    assert step >= 0 and addition >= 0
    return os.path.join(self.tmp_img_dir, f'step-{step:04d}.{addition:04d}.png')

  def render(self,save_step:int=None,save_frame:bool=False,visualize:bool=False, **kwargs) -> str:
    """
    either saves to file or visualizes the partial planners current state
    can pass in a post_render function (fig,ax)->(new_fig,new_ax) to modify the image before saving/viewing
    save_step: int, optional - if not None, save the image to tmp_img_dir with the given step number
    save_frame: bool, optional - if True, save the image to tmp_img_dir as an extra to the last saved step
    visualize: bool, optional - if True, display the image regardless of if it was saved to file
    """
    if 'fig_ax' in kwargs:
      fig,ax = kwargs.pop('fig_ax')
    else:
      fig,ax = plt.subplots()
    post_render = kwargs.pop('post_render', None)

    # render the planner state
    plot_planner(fig_ax=(fig,ax), world=self.world, observations=self.detected_obstacles, curr_pos=self.curr_pos, goal=self.x_goal, planned_path=self.planned_path, **self.get_render_kwargs(), **kwargs)
    # call postprocessing on the plt image if provided
    if post_render is not None:
      fig,ax = post_render(fig_ax=(fig,ax))

    if save_step is not None or save_frame: # save the image
      if save_step is not None: # given step number
        plt.savefig(self.tmp_img_path(save_step))
      elif save_frame: # addition to most recent step
        # get the last saved step
        last_step = 0
        while os.path.exists(self.tmp_img_path(last_step)):
          last_step += 1
        last_step -= 1
        if last_step == -1:
          last_step = 0
        # find next available step addition number
        extra_save_count = 1
        while os.path.exists(self.tmp_img_path(last_step, extra_save_count)):
          extra_save_count += 1
        # save the image
        plt.savefig(self.tmp_img_path(last_step, extra_save_count))
    # if visualize or not saving or displaying the nth step
    if visualize or (save_step is None and not save_frame) or (self.display_every_n >= 1 and (save_step % self.display_every_n == 0)):
      plt.show()
    plt.close()

  def save_gif(self) -> None:
    filenames = [os.path.join(self.tmp_img_dir, f) for f in sorted(os.listdir(self.tmp_img_dir)) if f.endswith('.png')]
    if len(filenames) == 0:
      print('No images to save into gif...')
      return
    # print('Rending gif from files: ', filenames)

    delay = 10
    with imageio.get_writer(self.gif_output_path, mode='I') as writer:
      for i, filename in enumerate(filenames):
        image = imageio.imread(filename)
        # duplicate the first and last entires 10 times to create delay in gif
        need_delay = i == 0 or i == len(filenames)-1
        # delay step addition frames
        if filename[-8:-4] != '0000':
          need_delay = True

        for t in range(delay if need_delay else 1):
          writer.append_data(image)
    print(f'Saved gif to {self.gif_output_path}')

  def delete_tmps(self) -> None:
    shutil.rmtree(self.tmp_img_dir)
