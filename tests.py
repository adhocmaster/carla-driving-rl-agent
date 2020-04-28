"""Test cases"""

from agents import Specs
from agents.experiments import *
from agents.environment import SynchronousCARLAEnvironment


def test_carla_env():
    env = SynchronousCARLAEnvironment(debug=True)

    env.train(agent=Agents.evolutionary(env, max_episode_timesteps=200, update_frequency=64, batch_size=64, horizon=32),
              num_episodes=5, max_episode_timesteps=200, weights_dir=None, record_dir=None)


def test_baseline_env():
    env = BaselineExperiment(debug=True)
    env.train(agent=None, num_episodes=5, max_episode_timesteps=200, weights_dir=None, record_dir=None)


def test_pretrain_env(num_episodes=5):
    # Pretraining:
    for i in range(1, num_episodes + 1):
        print(f'Trace-{i}')
        experiment = CARLAPretrainExperiment(window_size=(670, 500), debug=True, vehicle_filter='vehicle.tesla.model3')
        experiment.train(agent=Agents.dummy.pretraining(experiment, max_episode_timesteps=256, speed=30,
                                                        traces_dir='data/traces/pretrain_tesla_batch256'),
                         num_episodes=1, max_episode_timesteps=256, record_dir=None, weights_dir=None, load_agent=False)


def test_sequence_layer():
    env = RouteFollowExperiment(debug=True)
    agent = Agents.criticless(env, max_episode_timesteps=768, filters=40,
                              preprocessing=dict(image=[dict(type='image', width=140, height=105, grayscale=True),
                                                        # dict(type='instance_normalization'),
                                                        dict(type='sequence', length=20)]))

    env.train(agent, num_episodes=5, max_episode_timesteps=768, weights_dir='criticless-sequence', record_dir=None)


def test_keyboard_agent(num_episodes=1, num_timesteps=1024, window_size=(670, 500), version=1):
    image_shape = (window_size[1], window_size[0], 3)
    if version == 1:
        env = CARLAPlayEnvironment(debug=True, image_shape=image_shape, vehicle_filter='vehicle.audi.*')
    elif version == 2:
        env = PLayEnvironment2(debug=True, image_shape=image_shape, vehicle_filter='vehicle.audi.*')
    else:
        env = PLayEnvironment3(debug=True, image_shape=image_shape, vehicle_filter='vehicle.audi.*')

    env.train(None, num_episodes=num_episodes, max_episode_timesteps=num_timesteps, weights_dir=None, record_dir=None)


# TODO: pretrain agent, then train
# TODO: adam vs evolutionary optimizer


def ppo_experiment(num_episodes: int, num_timesteps: int, batch_size=1, discount=0.99, learning_rate=3e-4, load=False,
                   image_shape=(105, 140, 3)):
    env = RouteFollowExperiment(debug=True, vehicle_filter='vehicle.tesla.model3', image_shape=image_shape,
                                window_size=(670, 500))

    ppo_agent = Agents.ppo(env, max_episode_timesteps=num_timesteps, batch_size=batch_size, discount=discount,
                           learning_rate=learning_rate, summarizer=Specs.summarizer(),
                           preprocessing=Specs.my_preprocessing(stack_images=10))

    env.train(agent=ppo_agent, num_episodes=num_episodes, max_episode_timesteps=num_timesteps, agent_name='ppo-agent',
              load_agent=load, record_dir=None)


def complete_state(num_episodes: int, num_timesteps: int, load=False, image_shape=(105, 140, 3), time_horizon=10,
                   optimization_steps=10):
    env = CompleteStateExperiment(debug=True, image_shape=image_shape, vehicle_filter='vehicle.tesla.model3',
                                  window_size=(670, 500), time_horizon=time_horizon)

    agent = Agents.ppo3(env, max_episode_timesteps=num_timesteps, time_horizon=time_horizon,
                        optimization_steps=optimization_steps,summarizer=Specs.summarizer())

    env.train(agent, num_episodes, max_episode_timesteps=num_timesteps, agent_name='ppo3-complete', load_agent=load,
              record_dir=None)


def carla_wrapper():
    from tensorforce.environments import CARLAEnvironment

    env = CARLAEnvironment(window_size=(670, 500), debug=True)
    agent = Agents.ppo(env, max_episode_timesteps=128)
    env.train(agent, num_episodes=5, max_episode_timesteps=128, weights_dir=None, record_dir=None)