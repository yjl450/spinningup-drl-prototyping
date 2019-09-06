import gym
import numpy as np
import torch
from torch import Tensor
import torch.nn as nn
import torch.optim as optim
import time
from spinup.algos.sac_pytorch.core_auto import TanhGaussianPolicySACAdapt, Mlp, soft_update_model1_with_model2, ReplayBuffer
from spinup.utils.logx import EpochLogger
from spinup.utils.run_utils import setup_logger_kwargs

"""
This one is the auto alpha version we now use, it doesn't do reparam action
This one probably will work??
From early results it seems that this one actually works
The results look similar to the results in the paper
"""

def sac_adapt(env_fn, hidden_sizes=[256, 256], seed=0,
              steps_per_epoch=5000, epochs=100, replay_size=int(1e6), gamma=0.99,
              polyak=0.995, lr=3e-4, alpha=0.2, batch_size=256, start_steps=10000,
              max_ep_len=1000, save_freq=1, dont_save=False, regularization_weight=1e-3,
              auto_alpha=True, use_one_step_version=False,
              logger_kwargs=dict(),):
    """
    Largely following OpenAI documentation
    But slightly different from tensorflow implementation
    Args:
        env_fn : A function which creates a copy of the environment.
            The environment must satisfy the OpenAI Gym API.

        hidden_sizes: number of entries is number of hidden layers
            each entry in this list indicate the size of that hidden layer.
            applies to all networks

        seed (int): Seed for random number generators.

        steps_per_epoch (int): Number of steps of interaction (state-action pairs)
            for the agent and the environment in each epoch. Note the epoch here is just logging epoch
            so every this many steps a logging to stdouot and also output file will happen
            note: not to be confused with training epoch which is a term used often in literature for all kinds of
            different things

        epochs (int): Number of epochs to run and train agent. Usage of this term can be different in different
            algorithms, use caution. Here every epoch you get new logs

        replay_size (int): Maximum length of replay buffer.

        gamma (float): Discount factor. (Always between 0 and 1.)

        polyak (float): Interpolation factor in polyak averaging for target
            networks. Target networks are updated towards main networks
            according to:

            .. math:: \\theta_{\\text{targ}} \\leftarrow
                \\rho \\theta_{\\text{targ}} + (1-\\rho) \\theta

            where :math:`\\rho` is polyak. (Always between 0 and 1, usually
            close to 1.)

        lr (float): Learning rate (used for both policy and value learning).

        alpha (float): Entropy regularization coefficient. (Equivalent to
            inverse of reward scale in the original SAC paper.)

        batch_size (int): Minibatch size for SGD.

        start_steps (int): Number of steps for uniform-random action selection,
            before running real policy. Helps exploration. However during testing the action always come from policy

        max_ep_len (int): Maximum length of trajectory / episode / rollout. Environment will get reseted if
        timestep in an episode excedding this number

        save_freq (int): How often (in terms of gap between epochs) to save
            the current policy and value function.

        logger_kwargs (dict): Keyword args for EpochLogger.

    """

    """set up logger"""
    logger = EpochLogger(**logger_kwargs)
    logger.save_config(locals())

    env, test_env = env_fn(), env_fn()

    ## seed torch and numpy
    torch.manual_seed(seed)
    np.random.seed(seed)

    ## seed environment along with env action space so that everything about env is seeded
    env.seed(seed)
    env.action_space.np_random.seed(seed)
    test_env.seed(seed)
    test_env.action_space.np_random.seed(seed)

    obs_dim = env.observation_space.shape[0]
    act_dim = env.action_space.shape[0]

    # if environment has a smaller max episode length, then use the environment's max episode length
    max_ep_len = env._max_episode_steps if max_ep_len > env._max_episode_steps else max_ep_len

    # Action limit for clamping: critically, assumes all dimensions share the same bound!
    # we need .item() to convert it from numpy float to python float
    act_limit = env.action_space.high[0].item()

    # Experience buffer
    replay_buffer = ReplayBuffer(obs_dim=obs_dim, act_dim=act_dim, size=replay_size)

    """
    Auto tuning alpha
    """
    if auto_alpha:
        target_entropy =  -np.prod(env.action_space.shape).item() # H
        log_alpha = torch.zeros(1, requires_grad=True)
        alpha_optim = optim.Adam([log_alpha], lr=lr)
    else:
        target_entropy, log_alpha, alpha_optim = None, None, None

    def test_agent(n=5):
        """
        This will test the agent's performance by running n episodes
        During the runs, the agent only take deterministic action, so the
        actions are not drawn from a distribution, but just use the mean
        :param n: number of episodes to run the agent
        """
        ep_return_list = np.zeros(n)
        for j in range(n):
            o, r, d, ep_ret, ep_len = test_env.reset(), 0, False, 0, 0
            while not (d or (ep_len == max_ep_len)):
                # Take deterministic actions at test time
                a = policy_net.get_env_action(o, deterministic=True, fixed_sigma=True)
                o, r, d, _ = test_env.step(a)
                ep_ret += r
                ep_len += 1
            ep_return_list[j] = ep_ret
            logger.store(TestEpRet=ep_ret, TestEpLen=ep_len)

    start_time = time.time()
    o, r, d, ep_ret, ep_len = env.reset(), 0, False, 0, 0
    total_steps = steps_per_epoch * epochs

    """init all networks"""
    # see line 1
    policy_net = TanhGaussianPolicySACAdapt(obs_dim, act_dim, hidden_sizes,action_limit=act_limit)
    q1_net = Mlp(obs_dim+act_dim,1,hidden_sizes)
    q2_net = Mlp(obs_dim+act_dim,1,hidden_sizes)

    q1_target_net = Mlp(obs_dim+act_dim,1,hidden_sizes)
    q2_target_net = Mlp(obs_dim+act_dim,1,hidden_sizes)

    # see line 2: copy parameters from value_net to target_value_net
    q1_target_net.load_state_dict(q1_net.state_dict())
    q2_target_net.load_state_dict(q2_net.state_dict())

    # set up optimizers
    policy_optimizer = optim.Adam(policy_net.parameters(),lr=lr)
    q1_optimizer = optim.Adam(q1_net.parameters(),lr=lr)
    q2_optimizer = optim.Adam(q2_net.parameters(),lr=lr)

    # mean squared error loss for v and q networks
    mse_criterion = nn.MSELoss()

    # Main loop: collect experience in env and update/log each epoch
    # NOTE: t here is the current number of total timesteps used
    # it is not the number of timesteps passed in the current episode
    for t in range(total_steps):
        """
        Until start_steps have elapsed, randomly sample actions
        from a uniform distribution for better exploration. Afterwards, 
        use the learned policy. 
        """
        if t > start_steps:
            a = policy_net.get_env_action(o, deterministic=False, fixed_sigma=True)
        else:
            a = env.action_space.sample()
        # Step the env, get next observation, reward and done signal
        o2, r, d, _ = env.step(a)
        ep_ret += r
        ep_len += 1

        # Ignore the "done" signal if it comes from hitting the time
        # horizon (that is, when it's an artificial terminal signal
        # that isn't based on the agent's state)
        d = False if ep_len == max_ep_len else d

        # Store experience (observation, action, reward, next observation, done) to replay buffer
        replay_buffer.store(o, a, r, o2, d)

        """
        one data one update part
        """
        if use_one_step_version and replay_buffer.size >= batch_size:
            # get data from replay buffer
            batch = replay_buffer.sample_batch(batch_size)
            obs_tensor = Tensor(batch['obs1'])
            obs_next_tensor = Tensor(batch['obs2'])
            acts_tensor = Tensor(batch['acts'])
            # unsqueeze is to make sure rewards and done tensors are of the shape nx1, instead of n
            # to prevent problems later
            rews_tensor = Tensor(batch['rews']).unsqueeze(1)
            done_tensor = Tensor(batch['done']).unsqueeze(1)

            """
            now we do a SAC update, following the OpenAI spinup doc
            check the openai sac document psudocode part for reference
            line nubmers indicate lines in psudocode part
            we will first compute each of the losses
            and then update all the networks in the end
            """
            # see line 12: get a_tilda, which is newly sampled action (not action from replay buffer)

            """get q loss"""
            with torch.no_grad():
                a_tilda_next, _, _, log_prob_a_tilda_next, _, _ = policy_net.forward(obs_next_tensor, fixed_sigma=True)
                q1_next = q1_target_net(torch.cat([obs_next_tensor, a_tilda_next], 1))
                q2_next = q2_target_net(torch.cat([obs_next_tensor, a_tilda_next], 1))

                min_next_q = torch.min(q1_next, q2_next) - alpha * log_prob_a_tilda_next
                y_q = rews_tensor + gamma * (1 - done_tensor) * min_next_q

            # JQ = 𝔼(st,at)~D[0.5(Q1(st,at) - r(st,at) - γ(𝔼st+1~p[V(st+1)]))^2]
            q1_prediction = q1_net(torch.cat([obs_tensor, acts_tensor], 1))
            q1_loss = mse_criterion(q1_prediction, y_q)
            q2_prediction = q2_net(torch.cat([obs_tensor, acts_tensor], 1))
            q2_loss = mse_criterion(q2_prediction, y_q)

            """
            get policy loss
            """
            a_tilda, mean_a_tilda, log_std_a_tilda, log_prob_a_tilda, _, _ = policy_net.forward(obs_tensor, fixed_sigma=True)

            # see line 12: second equation
            q1_a_tilda = q1_net(torch.cat([obs_tensor, a_tilda], 1))
            q2_a_tilda = q2_net(torch.cat([obs_tensor, a_tilda], 1))
            min_q1_q2_a_tilda = torch.min(q1_a_tilda, q2_a_tilda)

            # Jπ = 𝔼st∼D,εt∼N[α * logπ(f(εt;st)|st) − Q(st,f(εt;st))]
            policy_loss = (alpha * log_prob_a_tilda - min_q1_q2_a_tilda).mean()

            """
            add policy regularization loss, this is not in openai's minimal version, but
            they are in the original sac code, see https://github.com/vitchyr/rlkit for reference
            this part is not necessary but might improve performance
            NO LONGER USE REGULARIZATION IN SAC ADAPT, rlkit also removed this now
            """
            # policy_mean_reg_weight = regularization_weight
            # policy_std_reg_weight = regularization_weight
            # mean_reg_loss = policy_mean_reg_weight * (mean_a_tilda ** 2).mean()
            # std_reg_loss = policy_std_reg_weight * (log_std_a_tilda ** 2).mean()
            # policy_loss = policy_loss + mean_reg_loss + std_reg_loss

            """
            alpha loss, update alpha
            """
            if auto_alpha:
                alpha_loss = -(log_alpha * (log_prob_a_tilda + target_entropy).detach()).mean()

                alpha_optim.zero_grad()
                alpha_loss.backward()
                alpha_optim.step()

                alpha = log_alpha.exp().item()
            else:
                alpha_loss = 0

            """update networks"""
            q1_optimizer.zero_grad()
            q1_loss.backward()
            q1_optimizer.step()

            q2_optimizer.zero_grad()
            q2_loss.backward()
            q2_optimizer.step()

            policy_optimizer.zero_grad()
            policy_loss.backward()
            policy_optimizer.step()

            # see line 16: update target value network with value network
            soft_update_model1_with_model2(q1_target_net, q1_net, polyak)
            soft_update_model1_with_model2(q2_target_net, q2_net, polyak)

            # store diagnostic info to logger
            logger.store(LossPi=policy_loss.item(), LossQ1=q1_loss.item(), LossQ2=q2_loss.item(),
                         LossAlpha=alpha_loss.item(),
                         Q1Vals=q1_prediction.detach().numpy(),
                         Q2Vals=q2_prediction.detach().numpy(),
                         Alpha=alpha,
                         LogPi=log_prob_a_tilda.detach().numpy())

        # Super critical, easy to overlook step: make sure to update
        # most recent observation!
        o = o2
        if d or (ep_len == max_ep_len):
            """
            Perform all SAC updates at the end of the trajectory.
            This is a slight difference from the SAC specified in the
            original paper.
            Quoted from the original SAC paper: 'In practice, we take a single environment step
            followed by one or several gradient step' after a single environment step,
            the number of gradient steps is 1 for SAC. (see paper for reference)
            """
            for j in range(ep_len):
                if use_one_step_version:
                    break
                # get data from replay buffer
                batch = replay_buffer.sample_batch(batch_size)
                obs_tensor =  Tensor(batch['obs1'])
                obs_next_tensor =  Tensor(batch['obs2'])
                acts_tensor =  Tensor(batch['acts'])
                # unsqueeze is to make sure rewards and done tensors are of the shape nx1, instead of n
                # to prevent problems later
                rews_tensor =  Tensor(batch['rews']).unsqueeze(1)
                done_tensor =  Tensor(batch['done']).unsqueeze(1)

                """
                now we do a SAC update, following the OpenAI spinup doc
                check the openai sac document psudocode part for reference
                line nubmers indicate lines in psudocode part
                we will first compute each of the losses
                and then update all the networks in the end
                """
                # see line 12: get a_tilda, which is newly sampled action (not action from replay buffer)

                """get q loss"""
                with torch.no_grad():
                    a_tilda_next, _, _, log_prob_a_tilda_next, _, _ = policy_net.forward(obs_next_tensor, fixed_sigma=True)
                    q1_next = q1_target_net(torch.cat([obs_next_tensor,a_tilda_next], 1))
                    q2_next = q2_target_net(torch.cat([obs_next_tensor,a_tilda_next], 1))

                    min_next_q = torch.min(q1_next,q2_next) - alpha * log_prob_a_tilda_next
                    y_q = rews_tensor + gamma*(1-done_tensor)*min_next_q

                # JQ = 𝔼(st,at)~D[0.5(Q1(st,at) - r(st,at) - γ(𝔼st+1~p[V(st+1)]))^2]
                q1_prediction = q1_net(torch.cat([obs_tensor, acts_tensor], 1))
                q1_loss = mse_criterion(q1_prediction, y_q)
                q2_prediction = q2_net(torch.cat([obs_tensor, acts_tensor], 1))
                q2_loss = mse_criterion(q2_prediction, y_q)

                """
                get policy loss
                """
                a_tilda, mean_a_tilda, log_std_a_tilda, log_prob_a_tilda, _, _ = policy_net.forward(obs_tensor, fixed_sigma=True)

                # see line 12: second equation
                q1_a_tilda = q1_net(torch.cat([obs_tensor,a_tilda],1))
                q2_a_tilda = q2_net(torch.cat([obs_tensor,a_tilda],1))
                min_q1_q2_a_tilda = torch.min(q1_a_tilda,q2_a_tilda)

                # Jπ = 𝔼st∼D,εt∼N[α * logπ(f(εt;st)|st) − Q(st,f(εt;st))]
                policy_loss = (alpha * log_prob_a_tilda - min_q1_q2_a_tilda).mean()

                """
                add policy regularization loss, this is not in openai's minimal version, but
                they are in the original sac code, see https://github.com/vitchyr/rlkit for reference
                this part is not necessary but might improve performance
                NO LONGER USE REGULARIZATION IN SAC ADAPT, rlkit also removed this now
                """
                # policy_mean_reg_weight = regularization_weight
                # policy_std_reg_weight = regularization_weight
                # mean_reg_loss = policy_mean_reg_weight * (mean_a_tilda ** 2).mean()
                # std_reg_loss = policy_std_reg_weight * (log_std_a_tilda ** 2).mean()
                # policy_loss = policy_loss + mean_reg_loss + std_reg_loss

                """
                alpha loss, update alpha
                """
                if auto_alpha:
                    alpha_loss = -(log_alpha * (log_prob_a_tilda + target_entropy).detach()).mean()

                    alpha_optim.zero_grad()
                    alpha_loss.backward()
                    alpha_optim.step()

                    alpha = log_alpha.exp().item()
                else:
                    alpha_loss = 0

                """update networks"""
                q1_optimizer.zero_grad()
                q1_loss.backward()
                q1_optimizer.step()

                q2_optimizer.zero_grad()
                q2_loss.backward()
                q2_optimizer.step()

                policy_optimizer.zero_grad()
                policy_loss.backward()
                policy_optimizer.step()

                # see line 16: update target value network with value network
                soft_update_model1_with_model2(q1_target_net, q1_net, polyak)
                soft_update_model1_with_model2(q2_target_net, q2_net, polyak)

                # store diagnostic info to logger
                logger.store(LossPi=policy_loss.item(), LossQ1=q1_loss.item(), LossQ2=q2_loss.item(),
                             LossAlpha=alpha_loss.item(),
                             Q1Vals=q1_prediction.detach().numpy(),
                             Q2Vals=q2_prediction.detach().numpy(),
                             Alpha=alpha,
                             LogPi=log_prob_a_tilda.detach().numpy())

            ## store episode return and length to logger
            logger.store(EpRet=ep_ret, EpLen=ep_len)
            ## reset environment
            o, r, d, ep_ret, ep_len = env.reset(), 0, False, 0, 0

        # End of epoch wrap-up
        if (t+1) % steps_per_epoch == 0:
            epoch = t // steps_per_epoch

            """
            Save pytorch model, very different from tensorflow version
            We need to save the environment, the state_dict of each network
            and also the state_dict of each optimizer
            """
            # if not dont_save: TODO save is disabled for now
            #     sac_state_dict = {'env':env,'policy_net':policy_net.state_dict(),
            #                     'target_value_net':target_value_net.state_dict(),
            #                       'q1_net':q1_net.state_dict(), 'q2_net':q2_net.state_dict(),
            #                       'policy_opt':policy_optimizer, 'value_opt':value_optimizer,
            #                       'q1_opt':q1_optimizer, 'q2_opt':q2_optimizer}
            #     if (epoch % save_freq == 0) or (epoch == epochs-1):
            #         logger.save_state(sac_state_dict, None)

            # Test the performance of the deterministic version of the agent.
            test_agent()

            # Log info about epoch
            logger.log_tabular('Epoch', epoch)
            logger.log_tabular('EpRet', with_min_and_max=True)
            logger.log_tabular('TestEpRet', with_min_and_max=True)
            logger.log_tabular('EpLen', average_only=True)
            logger.log_tabular('TestEpLen', average_only=True)
            logger.log_tabular('TotalEnvInteracts', t)
            logger.log_tabular('Q1Vals', with_min_and_max=True)
            logger.log_tabular('Q2Vals', with_min_and_max=True)
            logger.log_tabular('Alpha', with_min_and_max=True)
            logger.log_tabular('LossAlpha', average_only=True)
            logger.log_tabular('LogPi', with_min_and_max=True)
            logger.log_tabular('LossPi', average_only=True)
            logger.log_tabular('LossQ1', average_only=True)
            logger.log_tabular('LossQ2', average_only=True)
            logger.log_tabular('Time', time.time()-start_time)
            logger.dump_tabular()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=str, default='HalfCheetah-v2')
    parser.add_argument('--hid', type=int, default=256)
    parser.add_argument('--l', type=int, default=2)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--seed', '-s', type=int, default=0)
    parser.add_argument('--epochs', type=int, default=200)
    parser.add_argument('--exp_name', type=str, default='sac')
    parser.add_argument('--data_dir', type=str, default='data/')
    parser.add_argument('--steps_per_epoch', type=int, default=5000)
    args = parser.parse_args()

    ##TODO fix default hyperparam

    from spinup.utils.run_utils import setup_logger_kwargs
    logger_kwargs = setup_logger_kwargs(args.exp_name, args.seed)

    sac_adapt(lambda: gym.make(args.env), hidden_sizes=[args.hid] * args.l,
              gamma=args.gamma, seed=args.seed, epochs=args.epochs,
              steps_per_epoch=args.steps_per_epoch,
              logger_kwargs=logger_kwargs)