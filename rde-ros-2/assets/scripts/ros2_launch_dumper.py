# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import asyncio
import argparse
import os
import sys
import subprocess
from io import StringIO
from typing import cast
from typing import Dict
from typing import List
from typing import Text
from typing import Tuple
from typing import Union

from ament_index_python.packages import get_package_prefix
from ament_index_python.packages import PackageNotFoundError
from ros2launch.api import get_share_file_path_from_package
from ros2launch.api import is_launch_file
from ros2launch.api import launch_a_launch_file
from ros2launch.api import LaunchFileNameCompleter
from ros2launch.api import MultipleLaunchFilesError
from ros2launch.api import print_a_launch_file
from ros2launch.api import print_arguments_of_launch_file
import launch
from launch.launch_description_sources import get_launch_description_from_any_launch_file
from launch.utilities import is_a
from launch.utilities import normalize_to_list_of_substitutions
from launch.utilities import perform_substitutions
from launch import Action
from launch.actions import ExecuteProcess
from launch.actions import IncludeLaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.launch_context import LaunchContext
from launch_ros.actions import PushRosNamespace
from launch_ros.ros_adapters import get_ros_adapter


def parse_launch_arguments(launch_arguments: List[Text]) -> List[Tuple[Text, Text]]:
    """Parse the given launch arguments from the command line, into list of tuples for launch."""
    parsed_launch_arguments = {}  # type: ignore; 3.7+ dict is ordered.
    for argument in launch_arguments:
        count = argument.count(':=')
        if count == 0 or argument.startswith(':=') or (count == 1 and argument.endswith(':=')):
            raise RuntimeError(
                "malformed launch argument '{}', expected format '<name>:=<value>'"
                .format(argument))
        name, value = argument.split(':=', maxsplit=1)
        parsed_launch_arguments[name] = value  # last one wins is intentional
    return parsed_launch_arguments.items()


def find_files(file_name):
    """Find executable file in PATH or return the original name if not found."""
    try:
        if os.name == 'nt':
            # Windows
            output = subprocess.Popen(['where', file_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True).communicate()[0]
            output = output.decode().strip()
            if output:
                search_results = output.split(os.linesep)
                return search_results[0] if search_results[0] else file_name
        else:
            # Unix-like systems
            output = subprocess.Popen(['which', file_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()[0]
            output = output.decode().strip()
            if output:
                return output
    except Exception:
        # If any error occurs, return the original file name
        pass
    
    return file_name


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process some integers.')
    arg = parser.add_argument(
        'launch_full_path',
        help='Full file path to the launch file')
    arg = parser.add_argument(
        'launch_arguments',
        nargs='*',
        help="Arguments to the launch file; '<name>:=<value>' (for duplicates, last one wins)")

    args = parser.parse_args()

    path = None
    launch_arguments = []

    if os.path.exists(args.launch_full_path):
        path = args.launch_full_path
    else:
        raise RuntimeError('No launch file supplied')

    launch_arguments.extend(args.launch_arguments)
    parsed_launch_arguments = parse_launch_arguments(launch_arguments)

    launch_description = launch.LaunchDescription([
        launch.actions.IncludeLaunchDescription(
            launch.launch_description_sources.AnyLaunchDescriptionSource(
                path
            ),
            launch_arguments=parsed_launch_arguments,
        ),
    ])
    walker = []
    walker.append(launch_description)
    ros_specific_arguments: Dict[str, Union[str, List[str]]] = {}
    context = LaunchContext(argv=launch_arguments)
    
    # Handle asyncio event loop properly for newer Python versions
    try:
        # Try to get the current event loop
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop, create a new one
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    context._set_asyncio_loop(loop)

    try:
        # * Here we mimic the run loop inside launch_service,
        #   but without actually kicking off the processes.
        # * Traverse the sub entities by DFS.
        # * Shadow the stdout to avoid random print outputs.
        mystring = StringIO()
        sys.stdout = mystring
        while walker:
            entity = walker.pop()
            try:
                visit_future = entity.visit(context)
                if visit_future is not None:
                    visit_future.reverse()
                    walker.extend(visit_future)
            except Exception as visit_ex:
                # Log visit errors but continue processing
                sys.stdout = sys.__stdout__
                print(f"Warning: Error visiting entity {type(entity).__name__}: {visit_ex}", file=sys.stderr)
                sys.stdout = mystring
                continue

                   
            if is_a(entity, ExecuteProcess):
                typed_action = cast(ExecuteProcess, entity)
                if typed_action.process_details is not None:
                    sys.stdout = sys.__stdout__
                    # Prefix this with a tab character so the caller knows this is a line to be processed.
                    commands = ['\t']
                    
                    # Process the command properly, handling substitutions
                    cmd_list = typed_action.process_details['cmd']
                    if cmd_list:
                        # Handle the first command (executable)
                        first_cmd = cmd_list[0]
                        if hasattr(first_cmd, '__iter__') and not isinstance(first_cmd, str):
                            # Handle substitution objects
                            try:
                                first_cmd_resolved = perform_substitutions(context, normalize_to_list_of_substitutions(first_cmd))
                            except Exception:
                                first_cmd_resolved = str(first_cmd)
                        else:
                            first_cmd_resolved = str(first_cmd)
                        
                        # Check if executable exists in PATH
                        if os.sep not in first_cmd_resolved:
                            try:
                                first_cmd_resolved = find_files(first_cmd_resolved)
                            except Exception:
                                # If find_files fails, keep the original
                                pass
                        
                        commands.append('"{}"'.format(first_cmd_resolved))
                        
                        # Process remaining arguments
                        for cmd in cmd_list[1:]:
                            if hasattr(cmd, '__iter__') and not isinstance(cmd, str):
                                # Handle substitution objects
                                try:
                                    cmd_resolved = perform_substitutions(context, normalize_to_list_of_substitutions(cmd))
                                except Exception:
                                    cmd_resolved = str(cmd)
                            else:
                                cmd_resolved = str(cmd)
                            
                            if cmd_resolved.strip():
                                commands.append('"{}"'.format(cmd_resolved.strip()))
                    
                    print(' '.join(commands))
                    sys.stdout = mystring

            # Lifecycle node support
            # https://github.com/ranchhandrobotics/rde-ros-2/issues/632
            # Lifecycle nodes use a long running future to monitor the state of the nodes.
            # cancel this task, so that we can shut down the executor in ros_adapter
            try:
                async_future = entity.get_asyncio_future()
                if async_future is not None:
                    async_future.cancel()
            except Exception:
                # Some entities may not have asyncio futures, that's okay
                pass
        
    except Exception as ex:
        sys.stdout = sys.__stdout__
        print(f"Error processing launch file: {ex}", file=sys.stderr)
    finally:
        # Ensure stdout is restored
        sys.stdout = sys.__stdout__

    # Shutdown the ROS Adapter 
    # so that long running tasks shut down correctly in the debug bootstrap scenario.
    # https://github.com/ranchhandrobotics/rde-ros-2/issues/632
    ros_adapter = get_ros_adapter(context)
    if ros_adapter is not None:
        ros_adapter.shutdown()
