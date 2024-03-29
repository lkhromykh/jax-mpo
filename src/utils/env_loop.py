from typing import Callable, Tuple, Dict, TypedDict, List
from collections import deque, defaultdict
import copy

import jax
import dm_env
import numpy as np
import reverb

Action = Array = np.ndarray
Observation = Dict[str, Array]
Goals = Tuple[str, ...]
HERComputeReward = Callable[[Observation, Observation], bool]


class Trajectory(TypedDict, total=False):
    observations: List[Observation]
    actions: List[Action]
    rewards: List[float]
    discounts: List[float]
    next_observations: List[Observation]


class Every:

    def __init__(self, interval: int):
        self.interval = interval
        self._prev_step = 0

    def __call__(self, step: int) -> bool:
        assert step >= self._prev_step
        diff = step - self._prev_step
        if diff >= self.interval:
            self._prev_step = step
            return True
        return False


def environment_loop(env: dm_env.Environment,
                     policy: Callable[[Observation], Action],
                     prev_timestep: dm_env.TimeStep,
                     max_timesteps: int = float("inf"),
                     ) -> Tuple[Trajectory, dm_env.TimeStep]:
    step = 0
    trajectory = defaultdict(list)
    timestep = env.reset() if prev_timestep.last() else prev_timestep
    done = False
    while not done:
        step += 1
        obs = timestep.observation
        action = policy(obs)
        timestep = env.step(action)
        done = timestep.last() or (step >= max_timesteps)
        # o_tm1, a_tm1, r_t, discount_t
        trajectory["observations"].append(obs)
        trajectory["actions"].append(action)
        trajectory["rewards"].append(timestep.reward)
        trajectory["discounts"].append(timestep.discount)

    trajectory["observations"].append(timestep.observation)
    return trajectory, timestep


def n_step_fn(trajectory: Trajectory,
              n_step: int = 1,
              discount: float = .99
              ) -> Trajectory:
    """Computes N-step rewards for the trajectory.

    Do not use n_step_fn with together with augmentations
    due to ultimate off-policy regime.
    """
    trajectory = trajectory.copy()
    obs, rewards, disc = map(
        trajectory.get,
        ("observations", "rewards", "discounts")
    )
    # assert np.all(disc)
    # length = len(rewards)
    # discount_n = discount ** n_step
    # is_not_terminal = disc[-1]
    next_obs = obs[n_step:] + n_step * [obs[-1]]
    disc = [discount * d for d in disc]
    # discounts = \
    #     (length - n_step) * [discount_n] + \
    #     [is_not_terminal * discount ** i for i in range(n_step, 0, -1)]

    trajectory["next_observations"] = next_obs
    trajectory["discounts"] = disc

    if n_step == 1:
        return trajectory
    raise RuntimeError("while HER is in use avoid n-step.")

    n_step_rewards = []
    reward = 0
    prev_rewards = deque(n_step * [0.], maxlen=n_step)
    for r in reversed(rewards):
        stale_reward = prev_rewards.pop()
        reward = \
            r + discount * reward - discount_n * stale_reward
        prev_rewards.appendleft(r)
        n_step_rewards.append(reward)

    trajectory["rewards"] = n_step_rewards[::-1]
    return trajectory


def goal_augmentation(trajectory: Trajectory,
                      rng: np.random.Generator,
                      goal_sources: Goals,
                      goal_targets: Goals,
                      achieved: HERComputeReward,
                      strategy: str = "none",
                      discount: float = 1.,
                      amount: int = 1,
                      ) -> List[Trajectory]:
    """Augments source trajectory with additional goals."""
    length = len(trajectory["actions"])
    if strategy == "none":
        return [trajectory]

    trajectories = [trajectory]
    if strategy == "final":

        def hindsight_fn(achieved_obs, desired_obs):
            for gs, gt in zip(goal_sources, goal_targets):
                achieved_obs[gt] = desired_obs[gs]

        aug = copy.deepcopy(trajectory)
        final = aug["observations"][-1]
        for i in range(length):
            obs = aug["observations"][i]
            next_obs = aug["observations"][i+1]
            hindsight_fn(obs, final)
            is_achieved = float(achieved(next_obs, final))
            if (i == 0) and is_achieved:
                # If the task is solved from the beginning - ignore it.
                return trajectories
            aug["rewards"][i] = is_achieved
            # aug["discounts"][i] = 1. - is_achieved
        hindsight_fn(final, final)
        trajectories.extend(amount * [aug])
    elif strategy in ("future", "geom"):
        if strategy == "future":
            term_idx = rng.choice(length, size=amount) + 1
        else:
            discounts = discount * np.asarray(trajectory["discounts"])
            term_idx = sample_from_geometrical(rng, discounts, amount)
        term_idx = np.clip(term_idx, a_min=2, a_max=length)
        for idx in term_idx:
            tr = tree_slice(
                slice(0, idx), trajectory,
                is_leaf=lambda x: isinstance(x, list)
            )
            tr["observations"].append(trajectory["observations"][idx])
            aug = goal_augmentation(
                tr, rng, goal_sources, goal_targets, achieved, "final", 1)
            trajectories.append(aug[-1])
    else:
        raise ValueError(strategy)

    return trajectories


def _should_not_be_called(*args, **kwargs):
    raise RuntimeError("compute_reward_fn must be specified when using HER.")


class Adder:

    def __init__(self,
                 client: reverb.Client,
                 rng: np.random.Generator,
                 n_step: int = 1,
                 discount: float = .99,
                 goal_sources: Goals = (),
                 goal_targets: Goals = (),
                 predicate: HERComputeReward = _should_not_be_called,
                 aug_strategy: str = "none",
                 amount: int = 1
                 ):
        self._client = client
        self._rng = rng
        self._n_step_fn = lambda tr: n_step_fn(tr, n_step, discount)

        assert len(goal_sources) == len(goal_targets),\
            "Sources and targets must be paired."
        self._augmentation_fn = lambda tr, r: goal_augmentation(
            tr, r,
            goal_sources, goal_targets, predicate,
            aug_strategy, discount, amount
        )

    def __call__(self, trajectory: Trajectory):
        trajectories = self._augmentation_fn(trajectory, self._rng)
        trajectories = map(self._n_step_fn, trajectories)
        for tr in trajectories:
            self._insert(tr)

    def _insert(self, trajectory: Trajectory):
        with self._client.trajectory_writer(num_keep_alive_refs=1) as writer:
            for i in range(len(trajectory["actions"])):
                writer.append(
                    tree_slice(
                        i, trajectory,
                        is_leaf=lambda x: isinstance(x, list)
                    )
                )
                # o_tm1, a_tm1, r_t, d_t, o_t
                writer.create_item(
                    table="replay_buffer",
                    priority=1.,
                    trajectory=tree_slice(-1, writer.history)
                )
                writer.flush(block_until_num_items=20)


def tree_slice(sl, tree, is_leaf=None):
    return jax.tree_util.tree_map(
        lambda t: t[sl], tree, is_leaf=is_leaf
    )


def sample_from_geometrical(rng: np.random.Generator,
                            discount_t: np.ndarray,
                            size: int
                            ) -> np.ndarray:
    # P(t) ~ \prod^t_0 d_i * (1 - d_t)
    cont_prob_t = np.concatenate([
        np.ones_like(discount_t[:1]),
        discount_t
    ])
    term_prob_t = np.concatenate([
        1. - discount_t,
        np.ones_like(discount_t[-1:])
    ])
    cumprod_t = np.cumprod(cont_prob_t)
    prob_t = cumprod_t * term_prob_t
    return rng.choice(prob_t.size, size=size, p=prob_t)
