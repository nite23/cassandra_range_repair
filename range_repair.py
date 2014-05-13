#!/usr/bin/env python
"""
This script will allow for smaller repairs of Cassandra ranges.

#################################################
# success, ring_tokens, error = get_ring_tokens()
# success, host_token, error = get_host_token()
# range_termination = get_range_termination(host_token, ring_tokens)
# steps = 100

# print repr(is_murmur_ring(ring_tokens))
# print repr(get_ring_tokens())
# print repr(get_host_token())
# print repr(get_range_termination(host_token, ring_tokens))
# print repr(get_sub_range_generator(host_token, range_termination, steps).next())
#################################################
"""
from optparse import OptionParser

import logging
import operator
import optparse
import os
import re
import subprocess
import sys

def lrange(num1, num2=None, step=1):
    op = operator.__le__

    if num2 is None:
        num1, num2 = 0, num1
    if num2 < num1:
        if step > 0:
            num1 = num2
        op = operator.__gt__
    elif step < 0:
        num1 = num2

    while op(num1, num2):
        yield num1
        num1 += step

def run_command(command, *args):
    """take the created command and actually run it on the command
    line capturing the output
    """
    cmd = " ".join([command] + list(args))
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    return proc.returncode == 0, proc.returncode, cmd, stdout, stderr

def is_murmur_ring(ring):
    """check whether or not the ring is a Mumur3 ring
    :param ring: ring information
    """
    for i in ring:
        if i < 0:
            return True
    return False

def get_ring_tokens():
    """gets the token information for the ring
    """
    tokens = []
    logging.info("running nodetool ring, this will take a little bit of time")
    success, return_code, _, stdout, stderr = run_command("nodetool", "ring")

    if not success:
        return False, [], stderr

    logging.debug("ring tokens found, creating ring token list...")
    for line in stdout.split("\n")[6:]:
        segments = line.split()
        if len(segments) == 8:
            tokens.append(long(segments[-1]))

    return True, sorted(tokens), None

def get_host_tokens(host):
    cmd = ["nodetool"]
    if host:
        cmd.append("-h {host}".format(host=host))
    cmd.append("info -T")
    success, return_code, _, stdout, stderr = run_command(" ".join(cmd))
    if not success or stdout.find("Token") == -1:
        logging.error(stdout)
        return False, [], stderr
    token_list = []
    logging.debug("host tokens found, creating host token list...")
    for line in stdout.split("\n"):
        if not line.startswith("Token"): continue
        parts = line.split()
        token_list.append(long(parts[2]))

    return True, token_list, None

def get_range_termination(token, ring):
    """get the last/largest token in the ring
    """
    for i in ring:
        if token < i:
            return i
    # token is the largest value in the ring.  Since the rings wrap around,
    # return the first value.
    return ring[0]

def get_sub_range_generator(start, stop, steps=100):
    """Generate $step subranges between $start and $stop
    :param start: beginning token in the range
    :param stop: ending token in the range
    :param step: number of sub-ranges to create

    There is special-case handling for when there are more steps than there
    are keys in the range: just return the start and stop values.
    """
    if start+steps+1 < stop:
        step_increment = abs(stop - start) / steps
        for i in lrange(start + step_increment, stop + 1, step_increment):
            yield start, i
            start = i
        if start < stop:
            yield start, stop
    else:
        yield start, stop

def repair_range(host, keyspace, columnfamily, start, end):
    """Repair a keyspace/columnfamily between a given token range with nodetool
    :param keyspace: Cassandra keyspace to repair
    :param columnfamily: Cassandra Columnfamily to repair
    :param start: Beginning token in the range to repair
    :param end: Ending token in the range to repair
    """
    cmd = ["nodetool"]
    if host:
        cmd.append("-h {host}".format(host=host))
    cmd.append("repair {keyspace}".format(keyspace=keyspace))
    if columnfamily:
        cmd.append(columnfamily)
    cmd.append("-local -snapshot -pr -st {start} -et {end}".format(start=start, end=end))
    success, return_code, cmd, stdout, stderr = run_command(" ".join(cmd))
    return success, cmd, stdout, stderr

def setup_logging():
    """Sets up logging in a syslog format by log level
    """
    log_format = "%(levelname) -10s %(asctime)s %(funcName) -20s line:%(lineno) -5d: %(message)s"
    log_level = os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(level=logging.getLevelName(log_level), format=log_format)

def format_murmur(num):
    """Format a number for Murmur3
    :param integer: Murmr3 number to be formatted
    """
    return "{0:020d}".format(num)

def format_md5(num):
    """Format a number for RandomPartitioner
    :param integer: RandomPartitioner number to be formatted
    """
    return "{0:039d}".format(num)

def repair(keyspace, columnfamily, host, start_steps=100):
    """Repair a keyspace/columnfamily by breaking each token range into $start_steps ranges
    :param keyspace: cassandra keyspace to repair
    :param start_steps: break range to repair in to $start_steps (default:100)
    """
    success, ring_tokens, error = get_ring_tokens()
    if not success:
        logging.error("Error fetching ring tokens: {0}".format(error))
        return False

    success, host_token_list, error = get_host_tokens(host)
    if not success:
        logging.error("Error fetching host token: {0}".format(error))
        return False

    total_tokens = len(host_token_list)
    for token_num, host_token in enumerate(host_token_list):
        steps = start_steps
        range_termination = get_range_termination(host_token, ring_tokens)
        formatter = format_murmur if is_murmur_ring(ring_tokens) else format_md5

        logging.info(
            "[{count}/{total}] repairing range ({token}, {termination}) in {steps} steps for keyspace {keyspace}".format(
                count=token_num + 1,
                total=total_tokens,
                token=formatter(host_token), 
                termination=formatter(range_termination), 
                steps=steps, 
                keyspace=keyspace))

        for start, end in get_sub_range_generator(host_token, range_termination, steps):
            start = formatter(start)
            end = formatter(end)

            logging.debug(
                "step {steps:04d} repairing range ({start}, {end}) for keyspace {keyspace}".format(
                    steps=steps,
                    start=start,
                    end=end,
                    keyspace=keyspace))

            success, cmd, stdout, stderr = repair_range(host, keyspace, columnfamily, start, end)
            if not success:
                logging.error("FAILED: {0}".format(cmd))
                logging.error(stderr)
                return False
            logging.debug("step {steps:04d} complete".format(steps=steps))
            steps -= 1

    return True

def main():
    """Check arguments and kick off repair
    """
    parser = OptionParser(add_help_option=False)
    parser.add_option("-k", "--keyspace", dest="keyspace",
                      help="Keyspace to repair", metavar="KEYSPACE")

    parser.add_option("-c", "--columnfamily", dest="cf", default=None,
                      help="ColumnFamily to repair", metavar="COLUMNFAMILY")

    parser.add_option("-h", "--host", dest="host",
                      help="Hostname to repair", metavar="HOST")

    parser.add_option("-s", "--steps", dest="steps", type="int", default=100,
                      help="Number of discrete ranges", metavar="STEPS")

    (options, args) = parser.parse_args()

    if not options.keyspace:
        parser.print_help()
        sys.exit(1)

    setup_logging()
    if repair(options.keyspace, options.cf, options.host, options.steps):
        sys.exit(0)

    sys.exit(2)

if __name__ == "__main__":
    main()
