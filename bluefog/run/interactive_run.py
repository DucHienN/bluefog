# Copyright 2020 Bluefog Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import argparse
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import traceback

import psutil
import bluefog
from bluefog.run import env_util, network_util, horovod_driver


BLUEFOG_TIMELINE = 'BLUEFOG_TIMELINE'
BLUEFOG_LOG_LEVEL = 'BLUEFOG_LOG_LEVEL'


def parse_args():

    override_args = set()

    parser = argparse.ArgumentParser(description='Bluefog Interactive Python Runner')

    parser.add_argument('-v', '--version', action="store_true", dest="version",
                        help="Shows bluefog version.")

    parser.add_argument('-np', '--num-proc', action="store", dest="np",
                        type=int, help="Total number of training processes.")

    parser.add_argument('-p', '--ssh-port', action="store", dest="ssh_port",
                        type=int, help="SSH port on all the hosts.")

    parser.add_argument('--daemonize', action="store_true", dest="daemonize",
                        help="Daemonize the ibfrun process")

    parser.add_argument('--network-interface', action='store', dest='nic',
                        help='Specify the network interface used for communication.')

    parser.add_argument('--use-infiniband', action="store_true", dest="use_infiniband",
                        help='If set, use inifiniband to communication instead of TCP.')

    parser.add_argument('--ipython-profile', action="store", dest="profile",
                        type=str, default="bluefog",
                        help="The profile name for ipython environment.")

    group_hosts_parent = parser.add_argument_group('host arguments')
    group_hosts = group_hosts_parent.add_mutually_exclusive_group()
    group_hosts.add_argument('-H', '--hosts', action='store', dest='hosts',
                             help='List of host names and the number of available slots '
                                  'for running processes on each, of the form: <hostname>:<slots> '
                                  '(e.g.: host1:2,host2:4,host3:1 indicating 2 processes can run '
                                  'on host1, 4 on host2, and 1 on host3). If not specified, '
                                  'defaults to using localhost:<np>')
    group_hosts.add_argument('-hostfile', '--hostfile', action='store', dest='hostfile',
                             help='Path to a host file containing the list of host names and '
                                  'the number of available slots. Each line of the file must be '
                                  'of the form: <hostname> slots=<slots>')

    parser.add_argument('--extra-mpi-flags', action="store", dest="extra_flags",
                        help='Extra mpi flages you want to pass for mpirun.')

    parser.add_argument('command', nargs=argparse.REMAINDER,
                        help="Command to be executed.")

    parsed_args = parser.parse_args()

    if not parsed_args.version and not parsed_args.np:
        parser.error('argument -np/--num-proc is required')

    return parsed_args



def main():
    args = parse_args()

    if args.version:
        print(bluefog.__version__)
        exit(0)

    hosts_arg, all_host_names = network_util.get_hosts_arg_and_hostnames(args)
    remote_host_names = network_util.filter_local_addresses(all_host_names)
    daemonize_arg = "--daemonize" if args.daemonize else ""

    if not env_util.is_open_mpi_installed():
        raise Exception(
            'ibfrun convenience script currently only supports Open MPI.\n\n'
            'Choose one of:\n'
            '1. Install Open MPI 4.0.0+ and re-install Bluefog.\n'
            '2. Run distributed '
            'training script using the standard way provided by your'
            ' MPI distribution (usually mpirun, srun, or jsrun).')

    if not env_util.is_ipyparallel_installed():
        raise Exception(
            'ibfrun is based on the ipyparallel package. Please install it in your\n'
            'system like `pip install ipyparallel` first, then run ibfrun again.'
        )

    env = os.environ.copy()
    env['BLUEFOG_CYCLE_TIME'] = str(20)  # Increase the cycle time
    if len(args.command) != 1 or args.command[0] not in ("start", "stop"):
        raise ValueError("The last command has to be either 'start' or 'stop', but it is "
                         "{} now.".format(args.command))
    command = args.command[0]
    # TODO(ybc) How to stop it properly?
    if not remote_host_names:
        ipcontroller_command = "ipcontroller --profile {profile}".format(
            profile=args.profile)
        ipengine_command = (
            "bfrun -np {np} ipengine {command} --profile {profile}".format(
                np=args.np,
                profile=args.profile,
                command=command
            )
        )
        if command == 'start':
            subprocess.run('ipcluster nbextension enable --user', shell=True, env=env)
            print(ipcontroller_command)
            subprocess.Popen(ipcontroller_command, shell=True, env=env)
            time.sleep(3)
        print(ipengine_command)
        subprocess.run(ipengine_command, shell=True, env=env)
        # os.execve('/bin/sh', ['/bin/sh', '-c', ipcluster_command], env)
        exit(0)

    # Following process assumes the users want to run over multiple machines.
    # TODO(ybc) Add support for multiple machines.
    raise RuntimeError("ibfrun does not support on multiple machine yet.")

    common_intfs = set()
    # 1. Check if we can ssh into all remote hosts successfully.
    assert network_util.check_all_hosts_ssh_successful(remote_host_names, args.ssh_port)
    if not args.nic:
        # 2. Find the set of common, routed interfaces on all the hosts (remote
        # and local) and specify it in the args. It is expected that the following
        # function will find at least one interface.
        # otherwise, it will raise an exception.
        # So far, we just use horovodrun to do this job since the task are the same.
        local_host_names = set(all_host_names) - set(remote_host_names)
        common_intfs = horovod_driver.driver_fn(all_host_names, local_host_names,
                                                args.ssh_port, args.verbose)
    else:
        common_intfs = [args.nic]

    tcp_intf_arg = '-mca btl_tcp_if_include {common_intfs}'.format(
        common_intfs=','.join(common_intfs)) if common_intfs else ''
    nccl_socket_intf_arg = '-x NCCL_SOCKET_IFNAME={common_intfs}'.format(
        common_intfs=','.join(common_intfs)) if common_intfs else ''

    if args.use_infiniband:
        ib_arg = "-mca btl openib,self"
    else:
        ib_arg = "-mca btl ^openib"

    if args.ssh_port:
        ssh_port_arg = "-mca plm_rsh_args \"-p {ssh_port}\"".format(
            ssh_port=args.ssh_port)
    else:
        ssh_port_arg = ""

    extra_flags = args.extra_flags if args.extra_flags else ''
    mpirun_command = (
        'mpirun --allow-run-as-root '
        '-np {num_proc} {hosts_arg} '
        '-bind-to none -map-by slot '
        '-mca pml ob1 {ib_arg} '
        '{ssh_port_arg} {tcp_intf_arg} '
        '{nccl_socket_intf_arg} '
        '{extra_flags} {env} {command}'
        .format(num_proc=args.np,
                hosts_arg=hosts_arg,
                ib_arg=ib_arg,
                ssh_port_arg=ssh_port_arg,
                tcp_intf_arg=tcp_intf_arg,
                nccl_socket_intf_arg=nccl_socket_intf_arg,
                extra_flags=extra_flags,
                env=' '.join('-x %s' % key for key in env.keys()
                             if env_util.is_exportable(key)),
                command=command)
    )

    if args.verbose:
        print(mpirun_command)
    # Execute the mpirun command.
    os.execve('/bin/sh', ['/bin/sh', '-c', mpirun_command], env)


if __name__ == "__main__":
    main()