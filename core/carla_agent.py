import os
import gym
import time
import random
import tensorflow as tf
import numpy as np

from typing import Union

from tensorflow.keras.optimizers.schedules import LearningRateSchedule
from tensorflow.keras import losses

from rl import utils
from rl import augmentations as aug
from rl.agents import PPOAgent
from rl.environments import ThreeCameraCARLAEnvironmentDiscrete
from rl.parameters import ConstantParameter, ParameterWrapper

from core.networks import CARLANetwork


class FakeCARLAEnvironment(gym.Env):
    """A testing-only environment with the same state- and action-space of a CARLA Environment"""

    def __init__(self):
        super().__init__()
        env = ThreeCameraCARLAEnvironmentDiscrete

        self.action_space = env.ACTION['space']
        self.observation_space = gym.spaces.Dict(road=env.ROAD_FEATURES['space'],
                                                 vehicle=env.VEHICLE_FEATURES['space'],
                                                 past_control=env.CONTROL['space'], command=env.COMMAND_SPACE,
                                                 image=gym.spaces.Box(low=-1.0, high=1.0, shape=(90, 360, 3)))

    def step(self, action):
        pass

    def reset(self):
        pass

    def render(self, mode='human'):
        pass


# -------------------------------------------------------------------------------------------------
# -- Agent
# -------------------------------------------------------------------------------------------------

class CARLAgent(PPOAgent):
    # Default neural network architecture
    DEFAULT_CONTROL = dict(units=256, num_layers=2, activation=utils.swish6)
    DEFAULT_CONTROL_VALUE = dict(units=256, num_layers=2, activation=utils.swish6)
    DEFAULT_DYNAMICS = dict(road=dict(units=32, normalization=None),
                            vehicle=dict(units=32, num_layers=2),
                            command=dict(units=16, normalization=None),
                            control=dict(units=16, num_layers=2),
                            shufflenet=dict(linear_units=0, g=1.0, last_channels=800),
                            agent=dict(units=16, num_layers=2),
                            image=dict(units=224))

    # TODO: experimental `eta` and `delta` coefficients for auxiliary losses
    def __init__(self, *args, aug_intensity=1.0, clip_norm=(1.0, 1.0, 1.0), name='carla', load_full=True, eta=0.0,
                 dynamics_lr: Union[float, LearningRateSchedule] = 1e-3, update_dynamics=False, delta=0.0, aux=1.0,
                 **kwargs):
        """
        :param aug_intensity: how much intense the augmentation should be.
        :param clip_norm: at which value the gradient's norm should be clipped.
        :param name: the agent's name, used for saving and loading.
        :param load_full: whether or not to load the full network (dynamics + value + policy) or just the dynamics net.
        :param eta: [experimental] scalar that encourages the agent to accelerate (see `policy_objective`)
        :param dynamics_lr: learning rate of the shared dynamics network.
        :param update_dynamics: whether or not to also update the shared network during a reinforcement learning update.
        :param delta: [experimental] scalar that penalizes the steering magnitude (see `policy_objective`)
        :param aux: use to balance auxiliary losses (speed + similarity loss).
        """
        assert aug_intensity >= 0.0

        # network specification
        network_spec = kwargs.pop('network', {})
        network_spec.setdefault('network', CARLANetwork)
        network_spec.setdefault('control_policy', self.DEFAULT_CONTROL)
        network_spec.setdefault('control_value', self.DEFAULT_CONTROL_VALUE)
        network_spec.setdefault('dynamics', self.DEFAULT_DYNAMICS)
        network_spec.setdefault('update_dynamics', update_dynamics)

        self.dynamics_path = os.path.join(kwargs.get('weights_dir', 'weights'), name, 'dynamics_model')
        self.load_full = load_full

        super().__init__(*args, name=name, network=network_spec, clip_norm=clip_norm, **kwargs)

        self.network: CARLANetwork = self.network
        self.aug_intensity = aug_intensity

        # self.delta = delta / self.batch_size
        # self.eta = eta / self.batch_size
        self.delta = delta
        self.eta = eta
        self.aux = aux

        # gradient clipping:
        if isinstance(clip_norm, float):
            self.should_clip_dynamics_grads = True
            self.grad_norm_dynamics = tf.constant(clip_norm, dtype=tf.float32)

        elif isinstance(clip_norm[2], float):
            self.should_clip_dynamics_grads = True
            self.grad_norm_dynamics = tf.constant(clip_norm[2], dtype=tf.float32)
        else:
            self.should_clip_dynamics_grads = False

        # optimizer
        self.dynamics_lr = super()._init_lr_schedule(lr=dynamics_lr)
        self.dynamics_lr.load(config=self.config.get('dynamics_lr', {}))

        self.dynamics_optimizer = utils.get_optimizer_by_name(name=kwargs.get('optimizer', 'adam'),
                                                              learning_rate=dynamics_lr)

    def act(self, state: dict, **kwargs):
        for k, v in kwargs.items():
            state[k] = v

        return self.network.act(state)

    def evaluate(self, episodes: int, timesteps: int, render=True, seeds=None) -> list:
        rewards = []
        sample_seed = False
        # preprocess_fn = self.preprocess()

        if isinstance(seeds, int):
            self.set_random_seed(seed=seeds)
        elif isinstance(seeds, list):
            sample_seed = True

        for episode in range(1, episodes + 1):
            if sample_seed:
                self.set_random_seed(seed=random.choice(seeds))

            preprocess_fn = self.preprocess()
            self.reset()
            episode_reward = 0.0

            state = self.env.reset()
            action = tf.zeros((1, self.num_actions))
            reward = 0.0
            value = tf.zeros_like(self.network.last_value)

            for t in range(1, timesteps + 1):
                if render:
                    self.env.render()

                if isinstance(state, dict):
                    state = {f'state_{k}': v for k, v in state.items()}

                state = preprocess_fn(state)
                state = utils.to_tensor(state)

                action, value = self.act(state, action=action, value=value, reward=tf.reshape(reward, (1, 1)))
                state, reward, done, _ = self.env.step(self.convert_action(action))
                episode_reward += reward

                self.log(actions=action, rewards=reward, values=value, terminals=done)

                if done or (t == timesteps):
                    print(f'Episode {episode} terminated after {t} timesteps with reward {episode_reward}.')
                    rewards.append(episode_reward)
                    break

            self.log(evaluation_reward=episode_reward)
            self.write_summaries()

        self.env.close()

        print(f'Mean reward: {round(np.mean(rewards), 2)}, std: {round(np.std(rewards), 2)}')
        return rewards

    def policy_batch_tensors(self) -> Union[tuple, dict]:
        """Defines a batch of data for the policy network"""
        states, advantages, actions, log_probabilities = super().policy_batch_tensors()
        states['value'] = self.memory.values[:-1]
        states['reward'] = self.memory.rewards[:-1]
        states['action'] = actions

        speed = utils.to_tensor(self.env.info_buffer['speed'], expand_axis=-1) / 100.0
        similarity = utils.to_tensor(self.env.info_buffer['similarity'], expand_axis=-1)

        return states, advantages, log_probabilities, speed, similarity

    def value_batch_tensors(self) -> Union[tuple, dict]:
        """Defines a batch of data for the value network"""
        states, returns = super().value_batch_tensors()
        states['value'] = self.memory.values[:-1]
        states['reward'] = self.memory.rewards[:-1]
        states['action'] = self.memory.actions

        speed = utils.to_tensor(self.env.info_buffer['speed'], expand_axis=-1) / 100.0
        similarity = utils.to_tensor(self.env.info_buffer['similarity'], expand_axis=-1)

        if speed.shape[0] >= returns.shape[0]:
            speed = speed[:returns.shape[0]]
            similarity = similarity[:returns.shape[0]]
        else:
            n = returns.shape[0] - speed.shape[0]
            speed = tf.concat([speed, tf.zeros((n, 1))], axis=1)
            similarity = tf.concat([similarity, tf.zeros((n, 1))], axis=1)

        return states, returns, speed, similarity

    def get_policy_gradients(self, batch):
        states, advantages, log_probabilities, speed, similarity = batch
        dynamics_out = self.network.dynamics_predict_train(states)
        new_batch = (dynamics_out, advantages, log_probabilities, speed, similarity)

        return super().get_policy_gradients(batch=new_batch)

    def policy_objective(self, batch):
        states, advantages, old_log_prob, true_speed, true_similarity = batch
        policy = self.network.policy(states, training=True)
        log_prob = policy['old_log_prob']
        entropy = tf.reduce_mean(policy['entropy'])
        speed = policy['speed']
        similarity = policy['similarity']

        # Entropy
        entropy_penalty = self.entropy_strength() * entropy

        # Compute the probability ratio between the current and old policy
        ratio = tf.math.exp(log_prob - old_log_prob)
        ratio = tf.reduce_mean(ratio, axis=1)  # mean over per-action ratio

        # Compute the clipped ratio times advantage
        clip_value = self.clip_ratio()
        min_adv = tf.where(advantages > 0.0, x=(1.0 + clip_value) * advantages, y=(1.0 - clip_value) * advantages)

        # aux losses
        speed_loss = 0.5 * tf.reduce_mean(losses.MSE(y_true=true_speed, y_pred=speed))
        similarity_loss = 0.5 * tf.reduce_mean(losses.MSE(y_true=true_similarity, y_pred=similarity))

        # total loss
        policy_loss = -tf.reduce_mean(tf.minimum(ratio * advantages, min_adv))
        total_loss = policy_loss - entropy_penalty + speed_loss + similarity_loss

        # penalize the magnitude of steering angle
        steer_angle = states['action'][:, 1]
        steer_penalty = self.delta * tf.reduce_sum(tf.square(steer_angle))

        # encourage to accelerate
        throttle = states['action'][:, 0]
        throttle = tf.where(throttle > 0.0, throttle, 0.0)
        throttle_penalty = self.eta * tf.reduce_sum(tf.square(1.0 - throttle))

        # Log stuff
        self.log(ratio=tf.reduce_mean(ratio), log_prob=tf.reduce_mean(log_prob), entropy=entropy,
                 entropy_coeff=self.entropy_strength.value, ratio_clip=clip_value, loss_speed_policy=speed_loss,
                 loss_policy=policy_loss, loss_entropy=entropy_penalty, speed_pi=tf.reduce_mean(speed),
                 loss_similarity_policy=similarity_loss, similarity_pi=tf.reduce_mean(similarity),
                 loss_steer=steer_penalty, loss_throttle=throttle_penalty)

        return total_loss + steer_penalty + throttle_penalty, 0.0

    def get_value_gradients(self, batch):
        states, returns, speed, similarity = batch
        dynamics_out = self.network.dynamics_predict_train(states)

        return super().get_value_gradients(batch=(dynamics_out, returns, speed, similarity))

    @tf.function
    def value_predict(self, inputs: dict) -> dict:
        return self.network.value(inputs, training=True)

    def value_objective(self, batch):
        states, returns, true_speed, true_similarity = batch
        prediction = self.value_predict(states)
        values, speed, similarity = prediction['value'], prediction['speed'], prediction['similarity']

        value_loss = 0.5 * tf.reduce_mean(losses.MSE(y_true=returns, y_pred=values))
        speed_loss = 0.5 * tf.reduce_mean(losses.MSE(y_true=true_speed, y_pred=speed))
        similarity_loss = 0.5 * tf.reduce_mean(losses.MSE(y_true=true_similarity, y_pred=similarity))

        self.log(speed_v=tf.reduce_mean(speed), similarity_v=tf.reduce_mean(similarity),
                 loss_v=value_loss, loss_speed_value=speed_loss, loss_similarity_value=similarity_loss)

        return value_loss + speed_loss + similarity_loss

    def get_value_batches(self):
        """Computes batches of data for updating the value network"""
        return utils.data_to_batches(tensors=self.value_batch_tensors(), batch_size=self.batch_size,
                                     drop_remainder=self.drop_batch_remainder, skip=self.skip_count,
                                     shuffle=self.shuffle, shuffle_batches=self.shuffle_batches,
                                     num_shards=self.obs_skipping)

    def imitation_learning(self, num_traces=np.inf, batch_size=None, shuffle_traces=True, alpha=1.0, beta=1.0, lr=1e-3,
                           clip_grad=1.0, optimizer='adam', save_every: Union[str, None] = 'end', epochs=1, seed=None,
                           validate_every=5, traces_dir='traces', shuffle_batches=False, shuffle_data=False,
                           accumulate_gradients=False, polyak=0.99):
        """Imitation leaning phase (with balanced-action batches)
        :param num_traces: number of traces to consider for each learning epoch.
        :param batch_size: dimension of the batch in terms of timesteps (i.e. single states)
        :param shuffle_traces: whether or not to shuffle the traces before loading them.
        :param alpha: scalar that balances the policy's loss
        :param beta: scalar that balances the value's loss
        :param lr: learning rate for the entire network (shared + policy + value)
        :param clip_grad: maximum gradient norm.
        :param optimizer: which optimizer to use. Default is "adam".
        :param save_every: whether or not to save the network at the end of each epoch.
        :param epochs: number of epochs. Each epoch lasts `num_traces` times.
        :param seed: random seed.
        :param validate_every: after how much traces validate the agent.
        :param traces_dir: the directory that contains the experience traces.
        :param shuffle_batches: whether or not to shuffle batches of data.
        :param shuffle_data: whether or not to shuffle the data before feeding the agent.
        :param accumulate_gradients: whether or not to accumulate (i.e. sum) the gradients over mini-batches of a
               single update.
        :param polyak: Polyak averaging coefficient.
        """
        print('== IMITATION LEARNING ==')

        batch_size = self.batch_size if batch_size is None else batch_size
        traces_dir = traces_dir if isinstance(traces_dir, str) else self.traces_dir
        self.set_random_seed(seed)
        self.network.imitation_model()

        if isinstance(lr, float):
            lr = ConstantParameter(lr)
        elif isinstance(lr, LearningRateSchedule):
            lr = ParameterWrapper(lr)

        optimizer = utils.get_optimizer_by_name(optimizer, learning_rate=lr)
        traces_count = utils.count_traces(traces_dir)

        for e in range(epochs):
            offset = 0

            while offset < traces_count:
                data, offset = self.imitation_prepare_data(batch_size, traces_dir, num_traces,
                                                           shuffle=shuffle_traces, offset=offset)
                preprocess_fn = self.augment()
                t0 = time.time()

                states, actions, rewards = data['state'], data['action'], data['reward']
                states: dict

                # remove speed and similarity from `states`
                speed = states.pop('speed') / 100.0
                similarity = states.pop('similarity')

                # also remove returns (base and exp) and use them to compute values
                returns_base = states.pop('returns_base')
                returns_exp = states.pop('returns_exp')
                states['value'] = tf.stack([returns_base, returns_exp], axis=1)

                if self.distribution_type == 'categorical':
                    states['action'] = self.convert_actions(actions)  # fix: data is collected in continuous space
                else:
                    states['action'] = actions

                states['reward'] = rewards
                aug_states = self.map_states(preprocess_fn, states)

                self.log(rewards=rewards, values_true_imitation=states['value'], lr_imitation=lr.value,
                         returns=returns_base * tf.pow(10, returns_exp),
                         returns_base=returns_base, returns_exp=returns_exp, speed=speed, similarity=similarity)
                self.log_actions(actions_imitation=actions, actions_true_imitation=states['action'],
                                 command=states['state_command'])

                # data = states
                data = (states, aug_states, speed, similarity)

                # validation and training:
                if (offset + 1) % validate_every == 0:
                    self.imitation_validation(data=data, batch_size=batch_size, seed=seed, alpha=alpha, beta=beta,
                                              shuffle_batches=shuffle_batches, shuffle_data=shuffle_data)

                self.imitation_update(data=data, batch_size=batch_size, alpha=alpha, beta=beta, polyak=polyak,
                                      clip_grad=clip_grad, optimizer=optimizer, shuffle_batches=shuffle_batches,
                                      shuffle_data=shuffle_data, accumulate_gradients=accumulate_gradients, seed=seed)
                self.write_summaries()
                print(f'[{e + 1}/{offset + 1}] Episode took {round(time.time() - t0, 3)}s.')

            lr.on_episode()

            if save_every == 'end':
                self.save()

    def imitation_prepare_data(self, batch_size: int, traces_dir: str, num_traces: int, shuffle=False,
                               offset=0) -> (dict, int):
        """Loads data from traces, and builds a batch with balanced actions (e.g. same amount of left and right
           steering etc.)
        """
        def filter_throttle(s, a, r):
            mask = a[:, 0] >= 0.0

            s = {k: utils.to_float(v)[mask] for k, v in s.items()}

            return s, a[mask], r[tf.concat([mask, [True]], axis=0)]

        def shuffle_trace(s: dict, a, r):
            indices = tf.range(start=0, limit=tf.shape(a)[0], dtype=tf.int32)
            indices = tf.random.shuffle(indices)

            for k, v in s.items():
                s[k] = tf.gather(v, indices)

            a = tf.gather(a, indices)
            r = tf.gather(r, tf.concat([indices, [tf.shape(r)[0] - 1]], axis=0))

            return s, a, r

        def mask_reward(r, mask):
            return r[tf.concat([mask, [True]], axis=0)]

        def filter_steering(s, a, r, t=0.1):
            masks = dict(left=a[:, 1] <= -t,
                         right=a[:, 1] >= t,
                         center=(a[:, 1] > -t) & (a[:, 1] < t))

            filtered_data = []

            for k in ['left', 'center', 'right']:
                mask = masks[k]
                taken = int(min(amounts[k], tf.reduce_sum(tf.cast(mask, tf.int32))))
                amounts[k] -= taken

                filtered_data.append(dict(state={k: v[mask][:taken] for k, v in s.items()},
                                          action=a[mask][:taken],
                                          reward=mask_reward(r, mask)[:taken]))
            return filtered_data

        amounts = dict(left=batch_size, right=batch_size, center=batch_size)
        data = None
        k = offset

        while sum(map(lambda k_: amounts[k_], amounts)) > 0:
            for j, trace in enumerate(utils.load_traces(traces_dir, max_amount=num_traces, shuffle=shuffle,
                                                        offset=0 if self.seed is None else offset)):
                k += 1
                trace = utils.unpack_trace(trace, unpack=False)

                states, actions = trace['state'], utils.to_float(trace['action'])
                rewards = utils.to_float(trace['reward'])
                states['speed'] = utils.to_tensor(trace['info_speed'], expand_axis=-1)
                states['similarity'] = utils.to_tensor(trace['info_similarity'], expand_axis=-1)
                states['state_command'] = self.convert_command(states['state_command'])

                # compute (decomposed) returns
                returns = utils.rewards_to_go(rewards, discount=self.gamma)
                states: dict
                states['returns_base'], \
                states['returns_exp'] = tf.map_fn(fn=utils.decompose_number, elems=utils.to_float(returns),
                                                  dtype=(tf.float32, tf.float32))

                states, actions, rewards = filter_throttle(states, actions, rewards)
                states, actions, rewards = shuffle_trace(states, actions, rewards)
                f_data = filter_steering(states, actions, rewards)

                if data is None:
                    data = f_data
                else:
                    for i, d in enumerate(f_data):
                        # for i in left, center, right...
                        data[i]['state'] = utils.concat_dict_tensor(data[i]['state'], d['state'])
                        data[i]['action'] = tf.concat([data[i]['action'], d['action']], axis=0)
                        data[i]['reward'] = tf.concat([data[i]['reward'], d['reward']], axis=0)

                if sum(map(lambda k_: amounts[k_], amounts)) <= 0:
                    break

        # concat left, center, and right parts together
        return dict(state=utils.concat_dict_tensor(*list(d['state'] for d in data)),
                    action=tf.concat(list(d['action'] for d in data), axis=0),
                    reward=tf.concat(list(d['reward'] for d in data), axis=0)), k

    # TODO: rename
    def log_actions(self, **kwargs):
        for tag, actions in kwargs.items():
            for action in actions:
                for i, a in enumerate(action):
                    self.log(**{f'{tag}-{i}': tf.squeeze(a)})

    def convert_actions(self, actions):
        return self.env.to_discrete(actions)

    @tf.function
    def map_states(self, fn: callable, states: dict):
        return tf.map_fn(fn=fn, elems=states)

    @staticmethod
    def convert_command(commands):
        def fn(c):
            i = tf.argmax(c)

            if i == 0:
                # left
                return tf.constant([1.0, 0.0, 0.0], dtype=tf.float32)

            if i == 1:
                # right
                return tf.constant([0.0, 0.0, 1.0], dtype=tf.float32)

            # straight or follow lane
            return tf.constant([0.0, 1.0, 0.0], dtype=tf.float32)

        return tf.map_fn(fn, elems=commands)

    def imitation_update(self, data, optimizer, batch_size: int, seed=None, alpha=0.5, beta=0.5, clip_grad=1.0,
                         shuffle_batches=False, shuffle_data=False, accumulate_gradients=False, polyak=0.99):
        t0 = time.time()
        batches = utils.data_to_batches(data, batch_size, shuffle=shuffle_data, seed=seed,
                                        drop_remainder=False, shuffle_batches=shuffle_batches,
                                        num_shards=self.obs_skipping)
        batch_gradients = None
        num_batches = 0

        for batch in batches:
            self.network.reset()
            num_batches += 1

            with tf.GradientTape() as tape:
                loss_policy, loss_value = self.imitation_objective(batch)
                total_loss = alpha * loss_policy + beta * loss_value

            gradients = tape.gradient(total_loss, self.network.imitation.trainable_variables)

            if accumulate_gradients:
                batch_gradients = utils.accumulate_gradients(gradients, batch_gradients)
            else:
                gradients = utils.clip_gradients(gradients, norm=clip_grad)

                if polyak < 1.0:
                    old_weights = self.network.imitation.get_weights()
                    optimizer.apply_gradients((zip(gradients, self.network.imitation.trainable_variables)))
                    utils.polyak_averaging(self.network.imitation, old_weights, alpha=polyak)
                else:
                    optimizer.apply_gradients((zip(gradients, self.network.imitation.trainable_variables)))

            self.log(loss_policy_imitation=loss_policy, loss_value_imitation=loss_value, loss_imitation=total_loss,
                     gradients_norm_imitation=[tf.norm(gradient) for gradient in gradients])

        if accumulate_gradients:
            gradients = utils.average_gradients(batch_gradients, num_batches)
            gradients = utils.clip_gradients(gradients, norm=clip_grad)

            if polyak < 1.0:
                old_weights = self.network.imitation.get_weights()
                optimizer.apply_gradients((zip(gradients, self.network.imitation.trainable_variables)))
                utils.polyak_averaging(self.network.imitation, old_weights, alpha=polyak)
            else:
                optimizer.apply_gradients((zip(gradients, self.network.imitation.trainable_variables)))

            self.log(gradients_norm_imitation_batch=[tf.norm(gradient) for gradient in gradients])

        print(f'[Imitation] update took {round(time.time() - t0, 3)}s.')

    def imitation_validation(self, data, batch_size: int, alpha: float, beta: float, seed=None, shuffle_batches=False,
                             shuffle_data=False):
        t0 = time.time()
        batches = utils.data_to_batches(data, batch_size, seed=seed, drop_remainder=False,
                                        shuffle=shuffle_data, shuffle_batches=shuffle_batches,
                                        num_shards=self.obs_skipping)
        self.network.reset()

        for batch in batches:
            loss_policy, loss_value = self.imitation_objective(batch, validation=True)
            total_loss = alpha * loss_policy + beta * loss_value

            self.log(validation_loss_policy=loss_policy, validation_loss_value=loss_value, validation_loss=total_loss)

        print(f'[Imitation] validation took {round(time.time() - t0, 3)}s.')

    def imitation_objective(self, batch, validation=False):
        """Imitation learning objective with `concordance loss` (i.e. a loss that encourages the network to make
           consistent predictions among augmented and non-augmented batches of data)
        """
        states, aug_states, speed, similarity = batch

        true_actions = utils.to_float(states['action'])
        true_values = states['value']

        # prediction on NON-augmented and AUGMENTED states
        policy, value = self.network.imitation_predict(states)
        policy_aug, value_aug = self.network.imitation_predict(aug_states)

        # actions, values, speed, and similarities
        actions, actions_aug = utils.to_float(policy['actions']), utils.to_float(policy_aug['actions'])
        values, values_aug = value['value'], value_aug['value']
        pi_speed, pi_speed_aug = policy['speed'], policy_aug['speed']
        v_speed, v_speed_aug = value['speed'], value_aug['speed']
        pi_similarity, pi_similarity_aug = policy['similarity'], policy_aug['similarity']
        v_similarity, v_similarity_aug = value['similarity'], value_aug['similarity']

        if not validation:
            self.log_actions(actions_pred_imitation=actions, actions_pred_aug_imitation=actions_aug)
            self.log(values_pred_imitation=values, values_pred_aug_imitation=values_aug,
                     speed_pi=pi_speed, speed_pi_aug=pi_speed_aug, speed_v=v_speed, speed_v_aug=v_speed_aug,
                     similarity_pi=pi_similarity, similarity_pi_aug=pi_similarity_aug,
                     similarity_v=v_similarity, similarity_v_aug=v_similarity_aug)

        # loss policy = sum of per-action MAE error
        loss_policy = (tf.reduce_mean(tf.reduce_sum(tf.abs(true_actions - actions), axis=1)) +
                       tf.reduce_mean(tf.reduce_sum(tf.abs(true_actions - actions_aug), axis=1))) / 2.0

        loss_value = (tf.reduce_mean(losses.MSE(y_true=true_values, y_pred=values)) +
                      tf.reduce_mean(losses.MSE(y_true=true_values, y_pred=values_aug))) / 2.0

        loss_speed_policy = (tf.reduce_mean(losses.MSE(y_true=speed, y_pred=pi_speed)) +
                             tf.reduce_mean(losses.MSE(y_true=speed, y_pred=pi_speed_aug))) / 2.0
        loss_speed_value = (tf.reduce_mean(losses.MSE(y_true=speed, y_pred=v_speed)) +
                            tf.reduce_mean(losses.MSE(y_true=speed, y_pred=v_speed_aug))) / 2.0

        loss_similarity_policy = (tf.reduce_mean(losses.MSE(y_true=similarity, y_pred=pi_similarity)) +
                                  tf.reduce_mean(losses.MSE(y_true=similarity, y_pred=pi_similarity_aug))) / 2.0
        loss_similarity_value = (tf.reduce_mean(losses.MSE(y_true=similarity, y_pred=v_similarity)) +
                                 tf.reduce_mean(losses.MSE(y_true=similarity, y_pred=v_similarity_aug))) / 2.0

        # concordance loss: make both prediction be close as possible
        concordance_policy = (tf.reduce_mean(losses.MSE(actions, actions_aug)) +
                              tf.reduce_mean(losses.MSE(pi_speed, pi_speed_aug)) +
                              tf.reduce_mean(losses.MSE(pi_similarity, pi_similarity_aug))) / 3.0

        concordance_value = (tf.reduce_mean(losses.MSE(values, values_aug)) +
                             tf.reduce_mean(losses.MSE(v_speed, v_speed_aug)) +
                             tf.reduce_mean(losses.MSE(v_similarity, v_similarity_aug))) / 3.0

        # total loss
        total_loss_policy = \
            loss_policy + self.aux * (loss_speed_policy + loss_similarity_policy) + self.delta * concordance_policy
        total_loss_value = \
            loss_value + self.aux * (loss_speed_value + loss_similarity_value) + self.eta * concordance_value

        if not validation:
            self.log(loss_policy=loss_policy, loss_value=loss_value, loss_speed_policy=loss_speed_policy,
                     loss_similarity_policy=loss_similarity_policy, loss_speed_value=loss_speed_value,
                     loss_similarity_value=loss_similarity_value,
                     loss_concordance_policy=concordance_policy, loss_concordance_value=concordance_value,
                     # loss_steer=steer_penalty, loss_throttle=throttle_penalty, loss_entropy=entropy_penalty
            )

        return total_loss_policy, total_loss_value

    def preprocess(self):
        """Augmentation function used during the reinforcement learning phase"""
        alpha = self.aug_intensity
        seed = self.seed

        @tf.function
        def augment_fn(state):
            state = state.copy()
            image = utils.to_float(state['state_image'])

            if alpha > 0:
                # Color distortion
                image = aug.simclr.color_distortion(image, strength=alpha, seed=seed)
                image = aug.tf_normalize(image)

            state['state_image'] = image
            return state

        return augment_fn

    def augment(self):
        """Augmentation function used during the imitation learning phase"""
        alpha = self.aug_intensity
        seed = self.seed

        @tf.function
        def augment_fn(state):
            state = state.copy()
            image = utils.to_float(state['state_image'])

            if alpha > 0:
                # Color distortion
                image = aug.simclr.color_distortion(image, strength=alpha, seed=seed)

                # blur
                if aug.tf_chance(seed=seed) < 0.33 * alpha:
                    image = aug.tf_gaussian_blur(image, size=5, seed=seed)

                # noise
                if aug.tf_chance(seed=seed) < 0.2 * alpha:
                    image = aug.tf_salt_and_pepper(image, amount=0.1)

                if aug.tf_chance(seed=seed) < 0.33 * alpha:
                    image = aug.tf_gaussian_noise(image, amount=0.15, std=0.15, seed=seed)

                image = aug.tf_normalize(image)

                # cutout
                if aug.tf_chance(seed=seed) < 0.15 * alpha:
                    image = aug.tf_cutout(image, size=6, seed=seed)

                # coarse dropout
                if aug.tf_chance(seed=seed) < 0.15 * alpha:
                    image = aug.tf_coarse_dropout(image, size=36, seed=seed)

            state['state_image'] = image
            return state

        return augment_fn

    def load_weights(self):
        print('loading weights...')
        self.network.load_weights(full=self.load_full)
