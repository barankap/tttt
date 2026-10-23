#!/usr/bin/env python3
"""Export Thanos routes and temporary SA tokens to XLSX. Python 3.6+, oc only.

The output contains credentials. Keep it private; never commit it or clusters.txt.
Creates/reuses an SA and a dedicated cluster-monitoring-view ClusterRoleBinding.
SA/RBAC persist after execution; tokens expire at the server-reported time.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from xml.sax.saxutils import escape

HEADERS = ['Cluster', 'API', 'Thanos_URL', 'Namespace', 'ServiceAccount',
           'Token', 'Expires_UTC', 'Status']


class StepError(Exception):
    pass


def run_oc(env, arguments, payload=None):
    command = ['oc'] + arguments + ['--request-timeout=30s']
    options = dict(env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if payload is None:
        options['stdin'] = subprocess.DEVNULL
    else:
        options['input'] = json.dumps(payload).encode('utf-8')
    result = subprocess.run(command, **options)
    if result.returncode:
        # Raw oc output/command can contain passwords or tokens: never print it.
        raise StepError('oc exited with code %d' % result.returncode)
    return result.stdout.decode('utf-8').strip()


def ensure_resources(env, namespace, sa):
    existing = run_oc(env, ['get', 'sa', sa, '-n', namespace,
                            '--ignore-not-found', '-o', 'json'])
    if not existing:
        run_oc(env, ['create', '-f', '-'], {
            'apiVersion': 'v1', 'kind': 'ServiceAccount',
            'metadata': {'name': sa, 'namespace': namespace},
        })
    binding_name = '%s-%s-monitoring-view' % (namespace, sa)
    role = {'apiGroup': 'rbac.authorization.k8s.io',
            'kind': 'ClusterRole', 'name': 'cluster-monitoring-view'}
    subject = {'kind': 'ServiceAccount', 'name': sa, 'namespace': namespace}
    existing = run_oc(env, ['get', 'clusterrolebinding', binding_name,
                            '--ignore-not-found', '-o', 'json'])
    if existing:
        obj = json.loads(existing)
        subjects = obj.get('subjects', [])
        same_subject = len(subjects) == 1 and all(
            subjects[0].get(key) == value for key, value in subject.items())
        if obj.get('roleRef') != role or not same_subject:
            raise StepError('existing binding differs; left unchanged')
    else:
        run_oc(env, ['create', '-f', '-'], {
            'apiVersion': 'rbac.authorization.k8s.io/v1',
            'kind': 'ClusterRoleBinding',
            'metadata': {'name': binding_name},
            'roleRef': role, 'subjects': [subject],
        })


def save_xlsx(path, records):
    """Write an actual XLSX using standard library; all cells are literal text."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    all_rows = [HEADERS] + records
    xml_rows = []
    for index, row in enumerate(all_rows, 1):
        cells = []
        for column, value in enumerate(row):
            text = str(value)
            if len(text) > 32767:
                raise ValueError('Excel cell exceeds 32767 characters')
            # XML 1.0 forbids control characters (except tab, LF and CR).
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
            ref = chr(65 + column) + str(index)
            cells.append('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                         % (ref, escape(text)))
        xml_rows.append('<row r="%d">%s</row>' % (index, ''.join(cells)))
    worksheet = ('<worksheet xmlns="%s"><sheetViews><sheetView workbookViewId="0">'
                 '<pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews>'
                 '<sheetData>%s</sheetData><autoFilter ref="A1:H%d"/></worksheet>'
                 % (ns, ''.join(xml_rows), len(all_rows)))
    fd, temporary = tempfile.mkstemp(prefix='.thanos-', dir=os.path.dirname(path))
    os.close(fd)  # mkstemp creates mode 0600 on Unix.
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as output:
            output.writestr('[Content_Types].xml',
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
            output.writestr('_rels/.rels',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            output.writestr('xl/workbook.xml',
                '<workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Thanos" sheetId="1" r:id="rId1"/></sheets></workbook>' % ns)
            output.writestr('xl/_rels/workbook.xml.rels',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
            output.writestr('xl/worksheets/sheet1.xml', worksheet)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('clusters', help='TXT: cluster-name https://api.example.invalid:6443')
    parser.add_argument('-u', '--username')
    parser.add_argument('-o', '--output', default='thanos_access.xlsx')
    parser.add_argument('--namespace', default='openshift-monitoring')
    parser.add_argument('--sa', default='thanos-exporter')
    parser.add_argument('--duration-hours', type=int, default=87600,
                        help='Requested token lifetime in hours (default: 87600 = 10 x 365 days); server decides actual expiry')
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument('--insecure', action='store_true')
    tls.add_argument('--ca', help='PEM CA bundle for oc connections')
    args = parser.parse_args()
    if not shutil.which('oc'):
        parser.error('oc command not found')
    if args.duration_hours < 1:
        parser.error('--duration-hours must be positive')
    for name in (args.namespace, args.sa):
        if len(name) > 63 or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', name):
            parser.error('Namespace and SA must be DNS labels of at most 63 characters')
    if not args.output.lower().endswith('.xlsx'):
        parser.error('Output must end with .xlsx')
    clusters = []
    with open(args.clusters, encoding='utf-8-sig') as source:
        for number, line in enumerate(source, 1):
            fields = line.split('#', 1)[0].split()
            if not fields:
                continue
            if len(fields) != 2 or not re.fullmatch(r'https://[^\s/@?#]+(?::[0-9]+)?/?', fields[1]):
                parser.error('Invalid cluster entry at line %d' % number)
            clusters.append(fields)
    if not clusters:
        parser.error('Cluster list is empty')
    path = os.path.abspath(args.output)
    # Reserve output exclusively. Existing reports are never overwritten.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    records = []
    save_xlsx(path, records)
    username = args.username or input('Username: ')
    password = getpass.getpass('Password: ')
    failures = 0
    with tempfile.TemporaryDirectory(prefix='oc-thanos-') as tmp:
        for index, (cluster, api) in enumerate(clusters):
            print('Cluster %d/%d' % (index + 1, len(clusters)), flush=True)
            env = dict(os.environ, KUBECONFIG=os.path.join(tmp, str(index)))
            row = [cluster, api, '', args.namespace, args.sa, '', '', '']
            stage = 'login'
            try:
                login = ['login', '--server=' + api, '--username=' + username,
                         '--password=' + password]
                if args.insecure:
                    login.append('--insecure-skip-tls-verify=true')
                elif args.ca:
                    login.append('--certificate-authority=' + os.path.abspath(args.ca))
                run_oc(env, login)
                stage = 'route'
                route = json.loads(run_oc(env, ['get', 'route', 'thanos-querier',
                                               '-n', 'openshift-monitoring', '-o', 'json']))
                host = route['spec']['host']
                if not host or not route['spec'].get('tls'):
                    raise StepError('HTTPS Thanos route not available')
                row[2] = 'https://' + host
                stage = 'SA/RBAC'
                ensure_resources(env, args.namespace, args.sa)
                stage = 'token'
                endpoint = '/api/v1/namespaces/%s/serviceaccounts/%s/token' % (args.namespace, args.sa)
                token = json.loads(run_oc(env, ['create', '--raw=' + endpoint, '-f', '-'], {
                    'apiVersion': 'authentication.k8s.io/v1', 'kind': 'TokenRequest',
                    'spec': {'expirationSeconds': args.duration_hours * 3600},
                }))['status']
                if not token.get('token') or not token.get('expirationTimestamp'):
                    raise StepError('incomplete TokenRequest response')
                row[5:8] = [token['token'], token['expirationTimestamp'], 'OK']
                print('  OK (token saved to XLSX)', flush=True)
            except (StepError, subprocess.TimeoutExpired, ValueError, KeyError, OSError) as error:
                row[7] = 'ERROR at %s: %s' % (stage, type(error).__name__)
                print('  ' + row[7], flush=True)
                failures += 1
            records.append(row)
            save_xlsx(path, records)  # Preserve completed clusters after interruption.
    print('XLSX: %s; failed clusters: %d' % (path, failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
