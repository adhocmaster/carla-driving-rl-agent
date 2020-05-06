"""Test cases"""

import os

from agents.experiments import *
from agents.environment import SynchronousCARLAEnvironment


def test_carla_env():
    env = SynchronousCARLAEnvironment(debug=True)

    env.train(agent=Agents.evolutionary(env, max_episode_timesteps=200, update_frequency=64, batch_size=64, horizon=32),
              num_episodes=5, max_episode_timesteps=200, weights_dir=None, record_dir=None)


def test_baseline_env():
    env = BaselineExperiment(debug=True)
    env.train(agent=None, num_episodes=5, max_episode_timesteps=200, weights_dir=None, record_dir=None)


# -------------------------------------------------------------------------------------------------
# -- Pretraining
# -------------------------------------------------------------------------------------------------

# TODO: vary weather, num npc, map, vehicle, ...
def collect_experience(num_episodes: int, num_timesteps: int, vehicle='vehicle.tesla.model3', image_shape=(105, 140, 3),
                       time_horizon=10, traces_dir='data/traces', ignore_traffic_light=False, **kwargs):
    env = CARLACollectExperience(window_size=(670, 500), debug=True, vehicle_filter=vehicle, time_horizon=time_horizon,
                                 image_shape=image_shape, **kwargs)

    # agent = Agents.pretraining(env, max_episode_timesteps=num_timesteps, speed=30.0, traces_dir=traces_dir)
    agent = Agents.behaviour_pretraining(env, max_episode_timesteps=num_timesteps, traces_dir=traces_dir,
                                         ignore_traffic_light=ignore_traffic_light)

    env.train(agent, num_episodes=num_episodes, max_episode_timesteps=num_timesteps, record_dir=None,
              weights_dir=None)


def pretrain_agent(agent, traces_dir: str, num_iterations: int, weights_dir: str, save_every=1, agent_name='pretrain'):
    assert save_every > 0

    for i in range(0, num_iterations, save_every):
        agent.pretrain(directory=traces_dir, num_iterations=save_every)

        env_utils.save_agent(agent, agent_name=agent_name, directory=weights_dir)
        print(f'Agent saved at {i + save_every}')


# TODO: measure -> vehicle transfer, weather transfer, scenario transfer (urban env with many/few/none npc), ...
def pretrain_then_train(num_episodes: int, num_timesteps: int, traces_dir: str, num_iterations: int, save_every=1,
                        image_shape=(105, 104, 3), vehicle='vehicle.tesla.model3', time_horizon=10, load=False,
                        window_size=(670, 500), skip_pretraining=False, weights_dir=None, record_dir=None, **kwargs):
    raise NotImplementedError('Deprecated!')
    env = CompleteStateExperiment(debug=True, image_shape=image_shape, vehicle_filter=vehicle, window_size=window_size,
                                  time_horizon=time_horizon, **kwargs)

    agent = Agents.ppo4(env, max_episode_timesteps=num_timesteps, time_horizon=time_horizon,
                        summarizer=Specs.summarizer())

    if not skip_pretraining:
        print('Start pretraining...')
        pretrain_agent(agent, traces_dir, num_iterations, weights_dir, save_every)
        print('Pretraining complete.')

    env.train(agent, num_episodes=num_episodes, max_episode_timesteps=num_timesteps, load_agent=load,
              weights_dir=weights_dir, record_dir=record_dir)


def alternate_training_with_pretraining(train_episodes: int, num_timesteps: int, traces_dir: str, repeat: int,
                                        pretrain_episodes: int, save_every=1, image_shape=(105, 104, 3),
                                        vehicle='vehicle.tesla.model3', time_horizon=10, load_agent=False,
                                        agent_name='carla-agent', weights_dir=None, window_size=(670, 500),
                                        record_dir=None, agent_args=None, **kwargs):
    """"""
    agent_args = dict() if agent_args is None else agent_args

    env = CompleteStateExperiment(debug=True, image_shape=image_shape, vehicle_filter=vehicle, window_size=window_size,
                                  time_horizon=time_horizon, **kwargs)

    if load_agent:
        agent = Agent.load(directory=os.path.join(weights_dir, agent_name), filename=agent_name, environment=env,
                           format='tensorflow')
        print('Agent loaded.')
    else:
        agent = Agents.ppo4(env, max_episode_timesteps=num_timesteps, time_horizon=time_horizon,
                            summarizer=Specs.summarizer(), **agent_args)
        print('Agent created.')

    for _ in range(repeat):
        # Train first (RL):
        env.train(agent, num_episodes=train_episodes, max_episode_timesteps=num_timesteps, weights_dir=weights_dir,
                  agent_name=agent_name, record_dir=record_dir)

        # Then pretrain (i.e. do imitation learning - IL):
        pretrain_agent(agent, traces_dir, num_iterations=pretrain_episodes, weights_dir=weights_dir,
                       agent_name=agent_name, save_every=save_every)

    env.close()


def alternate_training_with_pretraining2(train_episodes: int, num_timesteps: int, traces_dir: str, repeat: int,
                                         pretrain_episodes: int, save_every=1, image_shape=(105, 104, 3),
                                         vehicle='vehicle.tesla.model3', time_horizon=10, load_agent=False,
                                         agent_name='ppo5', weights_dir=None, window_size=(670, 500),
                                         record_dir=None, agent_args=None, **kwargs):
    """"""
    agent_args = dict() if agent_args is None else agent_args

    env = CompleteStateExperiment(debug=True, image_shape=image_shape, vehicle_filter=vehicle, window_size=window_size,
                                  time_horizon=time_horizon, **kwargs)

    if load_agent:
        agent = Agent.load(directory=os.path.join(weights_dir, agent_name), filename=agent_name, environment=env,
                           format='tensorflow')
        print('Agent loaded.')
    else:
        agent = Agents.ppo5(env, max_episode_timesteps=num_timesteps, summarizer=Specs.summarizer(),
                            saver=Specs.saver(os.path.join(weights_dir, agent_name), filename=agent_name), **agent_args)
        print('Agent created.')

    for _ in range(repeat):
        # Train first (RL):
        env.train(agent, num_episodes=train_episodes, max_episode_timesteps=num_timesteps, weights_dir=None,
                  agent_name=agent_name, record_dir=record_dir)

        # Then pretrain (i.e. do imitation learning - IL):
        pretrain_agent(agent, traces_dir, num_iterations=pretrain_episodes, weights_dir=weights_dir,
                       agent_name=agent_name, save_every=save_every)

    env.close()


# -------------------------------------------------------------------------------------------------

# TODO: broken!
def test_keyboard_agent(num_episodes=1, num_timesteps=1024, window_size=(670, 500), version=1):
    image_shape = (window_size[1], window_size[0], 3)

    if version == 1:
        env = CARLAPlayEnvironment(debug=True, image_shape=image_shape, vehicle_filter='vehicle.audi.*')
    elif version == 2:
        env = PlayEnvironment2(debug=True, image_shape=image_shape, vehicle_filter='vehicle.audi.*')
    else:
        env = PlayEnvironment3(debug=True, image_shape=image_shape, vehicle_filter='vehicle.audi.*')

    env.train(None, num_episodes=num_episodes, max_episode_timesteps=num_timesteps, weights_dir=None, record_dir=None)


def ppo_experiment(num_episodes: int, num_timesteps: int, batch_size=1, discount=0.99, learning_rate=3e-4, load=False,
                   image_shape=(105, 140, 3)):
    env = RouteFollowExperiment(debug=True, vehicle_filter='vehicle.tesla.model3', image_shape=image_shape,
                                window_size=(670, 500))

    ppo_agent = Agents.ppo(env, max_episode_timesteps=num_timesteps, batch_size=batch_size, discount=discount,
                           learning_rate=learning_rate, summarizer=Specs.summarizer(),
                           preprocessing=Specs.my_preprocessing(stack_images=10))

    env.train(agent=ppo_agent, num_episodes=num_episodes, max_episode_timesteps=num_timesteps, agent_name='ppo-agent',
              load_agent=load, record_dir=None)


def complete_state(num_episodes: int, num_timesteps: int, batch_size: int, image_shape=(105, 140, 3), time_horizon=10,
                   optimization_steps=10, **kwargs):
    env = CompleteStateExperiment(debug=True, image_shape=image_shape, vehicle_filter='vehicle.tesla.model3',
                                  window_size=(670, 500), time_horizon=time_horizon)

    agent = Agents.ppo6(env, max_episode_timesteps=num_timesteps, batch_size=batch_size,
                        optimization_steps=optimization_steps, **kwargs)

    env.train(agent, num_episodes, max_episode_timesteps=num_timesteps, agent_name='complete-state',
              weights_dir=None, record_dir=None)


# -------------------------------------------------------------------------------------------------
# -- Misc
# -------------------------------------------------------------------------------------------------

def carla_wrapper():
    from tensorforce.environments import CARLAEnvironment

    env = CARLAEnvironment(window_size=(670, 500), debug=True)
    agent = Agents.ppo(env, max_episode_timesteps=128)
    env.train(agent, num_episodes=5, max_episode_timesteps=128, weights_dir=None, record_dir=None)


def toy_example():
    net = Specs.networks.complex(networks=[
        Specs.networks.feature2d_skip(inputs='a', output='a_out', name='x', shape=(10, 10), kernel=(3, 3), filters=6,
                                      layers=4),
        Specs.networks.feature2d_skip(inputs='b', output='b_out', name='y', shape=(10, 17), kernel=(3, 4), filters=6,
                                      layers=4)
    ])
    print(net)

    states = dict(a=dict(type='float', shape=(10, 10)),
                  b=dict(type='float', shape=(10, 17)))

    agent = Agent.create(agent='ppo',
                         states=states,
                         actions=dict(type='float', shape=(3,), min_value=-1.0, max_value=1.0),
                         max_episode_timesteps=32,
                         batch_size=1,
                         network=net)

    print(agent.act(states=dict(a=np.random.rand(10, 10), b=np.random.rand(10, 17))))
