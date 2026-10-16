#!/usr/bin/env python3
"""Collect OpenShift master/infra capacity. Requires oc and Python 3."""
import argparse
import csv
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from decimal import Decimal


def quantity(value):
    match = re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)', str(value))
    if not match:
        raise ValueError('Invalid Kubernetes quantity: ' + str(value))
    suffix = match[2]
    factors = {'': 1, 'n': Decimal('1e-9'), 'u': Decimal('1e-6'),
               'm': Decimal('1e-3'), 'k': 10**3, 'K': 10**3,
               'M': 10**6, 'G': 10**9, 'T': 10**12, 'P': 10**15, 'E': 10**18}
    factors.update({s + 'i': 1024**i for i, s in enumerate('KMGTPE', 1)})
    return Decimal(match[1]) * factors[suffix]


def rows(cluster, api, data):
    for node in data['items']:
        labels = node['metadata'].get('labels', {})
        roles = []
        if any('node-role.kubernetes.io/' + r in labels for r in ('master', 'control-plane')):
            roles.append('master')
        if 'node-role.kubernetes.io/infra' in labels:
            roles.append('infra')
        if not roles:
            continue
        status = node['status']
        values = []
        for field in ('capacity', 'allocatable'):
            values.extend((str(quantity(status[field]['cpu'])),
                           format(quantity(status[field]['memory']) / (1024**3), '.2f')))
        yield [cluster, api, node['metadata']['name'], '+'.join(roles), *values]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('clusters', help='Text file: cluster-name https://api.example:6443 per line')
    parser.add_argument('-u', '--username')
    parser.add_argument('-o', '--output', default='cluster_resources.csv')
    parser.add_argument('--insecure', action='store_true', help='Disable TLS certificate verification')
    parser.add_argument('--ca', help='Trusted CA bundle PEM path for all clusters')
    args = parser.parse_args()
    clusters = []
    with open(args.clusters, encoding='utf-8') as source:
        for number, line in enumerate(source, 1):
            fields = line.split('#', 1)[0].split()
            if not fields:
                continue
            if len(fields) != 2 or not fields[1].startswith('https://'):
                parser.error('Invalid cluster entry on line %s' % number)
            clusters.append(fields)
    if not clusters:
        parser.error('Cluster list is empty')
    username = args.username or input('Username: ')
    password = getpass.getpass('Password: ')
    failures = 0
    # Exclusive creation prevents accidentally overwriting a previous report.
    with open(args.output, 'x', newline='', encoding='utf-8') as output:
        writer = csv.writer(output)
        writer.writerow(['cluster', 'api', 'node', 'role', 'cpu_capacity_cores',
                         'ram_capacity_GiB', 'cpu_allocatable_cores', 'ram_allocatable_GiB'])
        with tempfile.TemporaryDirectory(prefix='oc-resource-') as tmp:
            for index, (cluster, api) in enumerate(clusters):
                print('Reading: ' + cluster, file=sys.stderr)
                env = dict(os.environ, KUBECONFIG=os.path.join(tmp, str(index)))
                login = ['oc', 'login', api, '--username=' + username, '--password=' + password,
                         '--request-timeout=30s']
                if args.insecure:
                    login.append('--insecure-skip-tls-verify=true')
                elif args.ca:
                    login.append('--certificate-authority=' + os.path.abspath(args.ca))
                try:
                    # Do not print login output or exception command: they can contain credentials.
                    result = subprocess.run(login, env=env, capture_output=True, timeout=90)
                    if result.returncode:
                        print('ERROR: %s: login failed (credentials/TLS/connectivity).' % cluster, file=sys.stderr)
                        failures += 1
                        continue
                    result = subprocess.run(['oc', 'get', 'nodes', '-o', 'json', '--request-timeout=30s'],
                                            env=env, capture_output=True, timeout=60)
                    if result.returncode:
                        print('ERROR: %s: node query failed (RBAC/connectivity).' % cluster, file=sys.stderr)
                        failures += 1
                        continue
                    records = list(rows(cluster, api, json.loads(result.stdout)))
                    writer.writerows(records)
                    output.flush()
                    print('  %d nodes' % len(records), file=sys.stderr)
                except FileNotFoundError:
                    print('ERROR: oc command not found.', file=sys.stderr)
                    return 1
                except (subprocess.TimeoutExpired, ValueError, KeyError) as error:
                    print('ERROR: %s: %s' % (cluster, type(error).__name__), file=sys.stderr)
                    failures += 1
    print('Report: %s; failed clusters: %d' % (args.output, failures), file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
