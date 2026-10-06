"""Shared HW1 utilities compatible with the course server's Python 3.6."""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter
from decimal import Decimal
from functools import wraps
from pathlib import Path

from scheduler import runtime

ROOT = Path(os.environ['HW1_ROOT']).resolve()

DESIGNS = {
    'FXP_adder': ('FXP_adder', 1),
    'FXP_mul': ('FXP_mul', 1),
    'FLP_adder': ('FLP_adder', 1),
    'FLP_adder_7': ('FLP_adder', 7),
    'FLP_adder_4': ('FLP_adder', 4),
    'FLP_mul': ('FLP_mul', 1),
    'FLP_mul_3': ('FLP_mul', 3),
}

OPTS = ('Area', 'Delay', 'Between')
EXPORTS = ('netlist.v', 'timing.sdf', 'constraints.sdc', 'design.ddc')
DEFAULT_LIBRARY = '/cad/CBDK/ADFP/Executable_Package/Collaterals/IP/stdcell/N16ADFP_StdCell/CCS'
DEFAULT_CELL_MODEL = '/cad/CBDK/ADFP/Executable_Package/Collaterals/IP/stdcell/N16ADFP_StdCell/VERILOG/N16ADFP_StdCell.v'
SESSION = uuid.uuid4().hex[:12]
START = time.time()
STATE_LOCK = threading.RLock()


def state_locked(function):
    """helper function for shared state: lock an entire selection transaction."""
    @wraps(function)
    def locked(*arguments, **keywords):
        """helper function for selection transactions: serialize read and write."""
        with STATE_LOCK:
            return function(*arguments, **keywords)
    return locked


def phase(label):
    """helper function for progress output: separate workflow phases in the logs.

    The launcher also puts a blank line before this heading in quiet terminal
    output, while unrelated blank lines in tool output remain filtered.
    """
    runtime.emit('')
    logging.info('=== %s ===', label)


def require(test, text):
    """helper function for validation: raise RuntimeError when a condition fails."""
    if not test:
        raise RuntimeError(text)


def atomic(path, data):
    """helper function for saving state: replace a JSON file via a temporary file.

    Create parent directories and reject nonfinite JSON numbers. Readers see the
    previous file until the replacement is complete.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    temp.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')
    os.replace(temp, path)


def load(path, default=None):
    """helper function for reading state: load JSON or return default if absent.

    Invalid JSON raises an error rather than being treated as missing state.
    """
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def within(path, directory):
    """helper function for path checks: test containment using Python 3.6 APIs.

    This is a lexical test; callers resolve paths first when checking manifests.
    """
    # pathlib.is_relative_to is unavailable on older course-server Python.
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def digest(paths, extra):
    """helper function for fingerprints: hash configuration, file paths and bytes.

    Sort and deduplicate paths, require every file, and return a SHA-256 hex digest.
    Paths inside HW1_ROOT are recorded relative to the root.
    """
    h = hashlib.sha256(json.dumps(extra, sort_keys=True).encode())
    for p in sorted(set(map(Path, paths))):
        h.update(str(p.relative_to(ROOT) if within(p, ROOT) else p).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def validate_vectors(folder):
    """helper function for simulation: require all 100 supplied operand pairs.

    The TA testbench checks hex conversion and reports invalid or missing data.
    """
    for name in ('a.txt', 'b.txt'):
        path = ROOT / folder / name
        require(len(path.read_text().split()) == 100,
                f'{path}: expected 100 input values')


def rtl_files(design):
    """helper function for synthesis and simulation: find the design's RTL.

    Use files.txt order when provided; otherwise discover Verilog sources.
    DC and VCS perform HDL parsing, elaboration and top-module checks.
    """
    directory = ROOT / 'RTL' / design
    manifest = directory / 'files.txt'
    if manifest.is_file():
        names = [line.strip() for line in manifest.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        files = [(directory / name).resolve() for name in names]
        require(len(files) == len(set(files)), 'Duplicate sources in files.txt')
        for file in files:
            require(within(file, directory.resolve()) and file.suffix in ('.v', '.sv'),
                    f'Invalid RTL manifest entry: {file}')
    else:
        files = sorted(directory.rglob('*.v')) + sorted(directory.rglob('*.sv'))

    require(files, f'{design} is not implemented: put RTL in {directory}')
    return files


def env_config():
    """helper function for fingerprints: collect persistent HW1 environment settings."""
    transient = {'HW1_ROOT', 'HW1_DESIGN', 'HW1_OPT', 'HW1_PERIOD',
                 'HW1_ROUND', 'HW1_OUT', 'HW1_RTL_LIST'}
    return {key: value for key, value in os.environ.items()
            if key.startswith('HW1_') and key not in transient}


def synthesis_id(design, options):
    """helper function for stale-result checks: fingerprint RTL, scripts and DC settings."""
    library = Path(os.environ.get('HW1_LIBRARY_ROOT', DEFAULT_LIBRARY))
    stamps = {}
    for name in ('N16ADFP_StdCellss0p72vm40c_ccs.db',
                 'N16ADFP_StdCellff0p88v125c_ccs.db'):
        file = library / name
        stamps[str(file)] = ([file.stat().st_size, file.stat().st_mtime_ns]
                            if file.exists() else 'missing')

    directory = ROOT / 'RTL' / design
    files = rtl_files(design) + list(directory.rglob('*.vh')) + list(directory.rglob('*.svh'))
    if (directory / 'files.txt').exists():
        files.append(directory / 'files.txt')
    files += [ROOT / 'compile/dc.tcl', ROOT / 'scripts/run.sh', ROOT / 'scripts/workflow.py',
              ROOT / 'scripts/helper.py', ROOT / 'scripts/scheduler.py']
    return digest(files, {
        'design': design,
        'environment': env_config(),
        'library_file_identity': stamps,
        'timing_warnings_reviewed': options.input_delays_reviewed,
        'dc_executable': shutil.which('dcnxt_shell'),
    })


def verification_id(design, folder, period, options, wave=None):
    """helper function for simulation reuse: fingerprint test inputs and simulator settings."""
    cell = Path(os.environ.get('HW1_CELL_MODEL', DEFAULT_CELL_MODEL))
    files = rtl_files(design) + [ROOT / folder / name
                                 for name in ('testbench.v', 'a.txt', 'b.txt')]
    cell_identity = (digest([cell], {})
                     if folder == 'post_sim' and cell.exists() else str(cell))
    return digest(files, {
        'synthesis_id': synthesis_id(design, options),
        'schedule': 'TA_original',
        'pipeline_stages': DESIGNS[design][1],
        'period': str(Decimal(str(period)).normalize()),
        'wave': wave or options.wave,
        'simulator': shutil.which('vcs'),
        'corner': 'max',
        'cell_model': cell_identity,
    })


def remove_output(path):
    """helper function for cleanup: remove a generated file or directory in HW1.

    Resolve and check the target before recursive deletion. Never remove HW1
    itself, source/input folders, another project, or an external symlink target.
    """
    path = Path(path)
    resolved = path.resolve()
    require(within(resolved, ROOT), f'Cleanup target outside HW1: {path}')
    parts = resolved.relative_to(ROOT).parts
    require(len(parts) >= 2 and parts[0] in ('pre_sim', 'post_sim', 'gate_level', 'log')
            and parts[1] in DESIGNS,
            f'Cleanup target is not a generated output: {path}')
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def prepare_directory(path, replace=False):
    """helper function for output setup: create or replace a generated directory."""
    path = Path(path)
    if replace:
        remove_output(path)
    path.mkdir(parents=True, exist_ok=replace)
    return path


def prepare_log(path):
    """helper function for tool logs: create parents and truncate once per run.

    Compilation and execution then append through run_command to the same file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('')
    return path


def cleanup_pre_logs(design):
    """helper function for log migration: remove redundant strategy pre-sim copies.

    Keep the shared log/<design>/pre_sim.log and every synthesis/post-sim log.
    Run before dispatch so standalone, aggregated and resumed actions agree.
    """
    for opt in OPTS:
        remove_output(ROOT / 'log' / design / opt / 'pre_sim.log')


def reset_synthesis(design, opt):
    """helper function for fresh synthesis: replace one strategy's generated state.

    Clear its selection before deleting rounds, post-sim output and tool logs.
    Other strategies survive aggregated runs. Area/Delay changes invalidate the
    dependent Between selection. Resume never invokes this helper.
    """
    runtime.check_cancel()
    with STATE_LOCK:
        data = selected()
        entries = data['designs'].get(design, {})
        entries.pop(opt, None)
        if opt in ('Area', 'Delay'):
            entries.pop('Between', None)
        atomic(ROOT / 'result/selected_runs.json', data)
    logs = ROOT / 'log' / design
    # Migrate legacy flat synthesis/post logs before removing their directories.
    legacy = list(logs.glob(f'syth*_{opt}.log')) + [logs / f'post_sim_{opt}.log']
    for record in (ROOT / 'gate_level' / design / opt).glob('round*/metadata.json'):
        name = Path(load(record).get('log', '')).name
        if re.fullmatch(r'round\d+\.log', name):
            legacy.append(logs / name)
    for record in (ROOT / 'post_sim' / design / opt).glob('run*/verification.json'):
        name = Path(load(record).get('log', '')).name
        if re.fullmatch(r'post_sim\d+\.log', name):
            legacy.append(logs / name)
    for path in legacy:
        remove_output(path)
    prepare_directory(ROOT / 'gate_level' / design / opt, replace=True)
    prepare_directory(logs / opt, replace=True)
    remove_output(ROOT / 'post_sim' / design / opt)


def run_command(command, cwd, log, extra=None):
    """helper function for tool execution: run, stream and log an argument list.

    Restore the saved EDA library path for the child and apply extra environment
    values. Append the command and combined output to the tool log and stream
    them through the launcher. Fail on nonzero exit; terminate the process group
    if interrupted before completion.
    """
    environment = os.environ.copy()
    # Python starts with a clean loader path; restore the inherited EDA path only
    # for VCS, Design Compiler and simulator subprocesses, preserving empty/unset.
    loader_was_set = environment.pop('HW1_EDA_LD_LIBRARY_PATH_SET', None)
    loader_path = environment.pop('HW1_EDA_LD_LIBRARY_PATH', '')
    if loader_was_set == '1':
        environment['LD_LIBRARY_PATH'] = loader_path
    elif loader_was_set == '0':
        environment.pop('LD_LIBRARY_PATH', None)

    environment.update(extra or {})
    shown = ['env'] + [f'{k}={v}' for k, v in sorted((extra or {}).items())] + command if extra else command
    command_text = ' '.join(map(shlex.quote, command))
    header = '$ ' + ' '.join(map(shlex.quote, shown))
    logging.info('cwd=%s; log=%s', cwd, log.relative_to(ROOT))
    with Path(log).open('a') as sink:
        sink.write(header + '\n')
        sink.flush()
        runtime.emit(header, raw=True)
        with runtime.process_lock:
            runtime.check_cancel()
            process = subprocess.Popen(
                command, cwd=cwd, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, errors='replace', start_new_session=True,
            )
            runtime.register(process)
        try:
            for line in process.stdout:
                sink.write(line)
                sink.flush()
                runtime.emit(line, raw=True)
            rc = process.wait()
        finally:
            if process.poll() is None:
                runtime.terminate(process)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    runtime.terminate(process, force=True)
                    process.wait()
            process.stdout.close()
            with runtime.process_lock:
                runtime.processes.pop(process.pid, None)
    runtime.check_cancel()
    require(rc == 0, f'Command exited {rc}: {command_text}; see {log.relative_to(ROOT)}')


def interrupted(signum, frame):
    """helper function for signal handling: raise KeyboardInterrupt for cleanup."""
    if not runtime.stopped.is_set():
        runtime.stopped.set()
        raise KeyboardInterrupt(f'Signal {signum}')


def validate_sdf(annotation, simulation_text):
    """helper function for post-simulation: classify completed SDF annotation.

    Accept only SWC/IWSBA warning blocks explicitly saying delay will still be
    annotated. Keep all other SDF diagnostics and timing violations fatal. Counts
    come from the annotation file, not duplicate messages in the simulation log.
    Warning bodies may contain blank lines; finish at the next diagnostic or
    summary, and remove only text through the explicit annotation confirmation.
    """
    text = annotation.read_text(errors='replace')
    require('SDF annotation completed:' in text, 'SDF annotation did not complete')
    errors = re.findall(r'(?im)^\s*Total errors:\s*(\d+)\s*$', text)
    require(errors and all(int(value) == 0 for value in errors),
            'SDF annotation errors detected or error summary missing; see ' + str(annotation))
    counts = Counter()
    timing = r'timing violation|\$(?:setup|hold|recovery|removal|setuphold|recrem|width|period)\('
    timing_issue = re.search(timing, text + '\n' + simulation_text, re.I)
    require(timing_issue is None, 'Timing violation/check diagnostic: ' +
            (timing_issue.group() if timing_issue else '') + '; see ' + str(annotation))
    blocks = re.compile(
        r'(?ims)^[ \t]*Warning-\[(SDFCOM_[A-Z0-9_]+)\][^\n]*\n'
        r'.*?(?=^[ \t]*(?:Warning|Error|Fatal)(?:-\[|\s*:)|'
        r'^[ \t]*SDF\s+(?:Error|Fatal)\b|^[ \t]*Total (?:errors|warnings):|\Z)')

    def strip_connectivity_warnings(source, count=False):
        """helper function for SDF validation: remove explicitly accepted blocks."""
        def classify(match):
            """helper function for SDF validation: retain unknown/incomplete warnings."""
            code = match.group(1)
            confirmation = re.search(r'delay\s+will\s+still\s+be\s+annotated\.', match.group(), re.I)
            if code in ('SDFCOM_SWC', 'SDFCOM_IWSBA') and confirmation:
                if count:
                    counts[code] += 1
                # Retain anything following the accepted warning's confirmation.
                return match.group()[confirmation.end():]
            return match.group()

        return blocks.sub(classify, source)

    remaining = strip_connectivity_warnings(text, count=True)
    # VCS may truncate terminal warnings; the annotation file is checked in full.
    # Ignore only this exact informational notice in the tool output.
    simulation_text = re.sub(
        r'(?m)^[ \t]*All future warnings not reported; use \+sdfverbose to report them\.[ \t]*\r?$',
        '', simulation_text)
    remaining += '\n' + strip_connectivity_warnings(simulation_text)
    diagnostics = r'(?im)(?:warning|error|fatal).*\bSDF|\bSDF.*(?:warning|error|fatal)'
    issue = re.search(diagnostics, remaining)
    excerpt = ''
    if issue:
        line_start = remaining.rfind('\n', 0, issue.start()) + 1
        excerpt = remaining[line_start:issue.end() + 300].split('\n\n')[0].strip()
    require(issue is None, 'Unaccepted SDF annotation/timing diagnostic: ' +
            excerpt + '; see ' + str(annotation))
    warnings = re.findall(r'(?im)^\s*Total warnings:\s*(\d+)\s*$', text)
    require(warnings and int(warnings[-1]) == sum(counts.values()),
            'SDF warning summary contains unclassified warnings; see ' + str(annotation))
    if counts:
        logging.warning('SDF annotation completed with zero errors; accepted %d connectivity warnings (%s). '
                        'Full diagnostics: %s', sum(counts.values()),
                        ', '.join('{}={}'.format(code, count) for code, count in sorted(counts.items())),
                        annotation)
    return dict(counts)


def report_number(field, value):
    """helper function for report CSVs: round display values without changing metrics."""
    if value is None or value == '':
        return ''
    if field.endswith('_um2'):
        return format(float(value), '.3f')
    if field.endswith('_ns'):
        return format(float(value), '.4f')
    if field.endswith('_w'):
        return format(float(value), '.6g')
    return value


def timing_report(path):
    """helper function for report collection: read data arrival time in ns.

    Keep the positive arrival line before the required-time calculation, not
    its negative repetition in the slack subtraction. If a report contains
    several path groups, return the largest reported arrival and its endpoints.
    Read the library time unit from the same round; never assume ns.
    """
    units = (path.parent / 'units_report.txt').read_text()
    unit = re.search(r'Time_unit\s*:\s*([0-9.eE+-]+)\s*([A-Za-z]+)', units)
    require(unit is not None, 'Missing time unit in ' + str(path.parent / 'units_report.txt'))
    scales = {'second': 1e9, 'seconds': 1e9, 's': 1e9, 'ns': 1,
              'ps': 1e-3, 'fs': 1e-6, 'us': 1e3, 'ms': 1e6}
    suffix = unit.group(2).lower()
    require(suffix in scales, 'Unsupported timing-report unit: ' + suffix)
    scale = float(unit.group(1)) * scales[suffix]
    require(math.isfinite(scale) and scale > 0, 'Invalid timing-report unit')
    paths = []
    for block in path.read_text().split('Startpoint:')[1:]:
        arrival = re.search(r'^\s*data arrival time[ \t]+(\d+\.?\d*(?:[eE][+-]?\d+)?)\s*$',
                            block, re.MULTILINE)
        start = re.match(r'\s*(\S+)', block)
        end = re.search(r'^\s*Endpoint:\s*(\S+)', block, re.MULTILINE)
        if arrival and start and end:
            delay = float(arrival.group(1)) * scale
            require(math.isfinite(delay), 'Invalid arrival in ' + str(path))
            paths.append({'delay_ns': delay, 'startpoint': start.group(1),
                          'endpoint': end.group(1)})
    require(paths, 'No data arrival time/path in ' + str(path))
    return max(paths, key=lambda item: item['delay_ns'])


def metrics(path):
    """helper function for synthesis: load and validate DC's metrics.tsv record.

    Require the schema/completion markers and finite timing/area measurements.
    Convert numeric fields to floats; unavailable optional power values are None.
    """
    require(path.is_file(), f'Missing DC metrics: {path}')
    pairs = [line.split('\t', 1) for line in path.read_text().splitlines()]
    require(all((len(p) == 2 for p in pairs)), 'Malformed metrics.tsv')
    data = dict(pairs)
    require(data.get('schema') == '1' and data.get('complete') == '1', 'DC completion record absent')
    for k in ['period_ns', 'slack_ns', 'delay_ns', 'comb_um2', 'seq_um2', 'total_um2']:
        require(k in data and math.isfinite(float(data[k])), f'Invalid required metric {k}')
        data[k] = float(data[k])
    for k in ['dynamic_w', 'leakage_w', 'power_w']:
        data[k] = float(data[k]) if data.get(k) else None
        require(data[k] is None or (math.isfinite(data[k]) and data[k] >= 0), f'Invalid metric {k}')
    require(data['period_ns'] > 0 and data['delay_ns'] >= 0 and (data['total_um2'] >= 0), 'Invalid physical metrics')
    return data


@state_locked
def selected():
    """helper function for selections: load selected_runs.json or an empty map."""
    return load(ROOT / 'result/selected_runs.json', {'schema': 1, 'designs': {}})


def entry(design, opt):
    """helper function for selections: return a design/optimization entry or {}."""
    return selected()['designs'].get(design, {}).get(opt, {})


def preserve_meta(path, **updates):
    """helper function for attempt records: merge updates, save JSON and return it."""
    data = load(path, {}) or {}
    data.update(updates)
    atomic(path, data)
    return data


def artifacts_current(meta):
    """helper function for post-simulation and collection: check matching exported files."""
    hashes = meta.get('artifact_hashes', {})
    if not all(name in hashes for name in EXPORTS):
        return False
    for name, expected in hashes.items():
        path = ROOT / meta['path'] / name
        if not path.is_file() or hashlib.sha256((path).read_bytes()).hexdigest() != expected:
            return False
    return True


def verification_status(record, design, folder, period, options):
    """helper function for collection and resume: identify passed, stale or pending checks."""
    status = record.get('status', 'pending')
    if status == 'passed':
        current = verification_id(design, folder, period,
                                  options, wave=record.get('wave_format', options.wave))
        if record.get('verification_id') != current:
            status = 'stale'
    return status


def round_metadata(design, opt, number, options):
    """helper function for manual selection and post-simulation: load a current round."""
    path = ROOT / 'gate_level' / design / opt / f'round{number}' / 'metadata.json'
    meta = load(path)
    require(meta and meta['synthesis_id'] == synthesis_id(design, options), 'Round missing/stale')
    return meta


def search_config(design, opt, options):
    """helper function for tuning: capture the inputs and limits required to resume."""
    return {
        'identity': synthesis_id(design, options), 'optimization': opt,
        'tolerance': str(options.tolerance), 'resolution': str(options.resolution),
        'maximum': str(options.max_period), 'attempts': options.max_attempts,
        'environment': env_config(),
    }


def parse_args():
    """helper function for the launcher: parse commands and validate numeric limits."""
    parser = argparse.ArgumentParser(description='HW1 simulation, synthesis and collection')
    parser.add_argument('design', choices=list(DESIGNS) + ['all', 'collect'])
    parser.add_argument('action', nargs='?', choices=['pre', 'synth', 'tune', 'post', 'all', 'select'])
    parser.add_argument('optimization', nargs='?', choices=list(OPTS) + ['all'])
    parser.add_argument('--period', type=Decimal)
    parser.add_argument('--jobs', type=int, default=1,
                        help='Maximum simultaneous workflow tasks (default: 1)')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--round', type=int)
    parser.add_argument('--max-attempts', type=int, default=20)
    parser.add_argument('--tolerance', type=Decimal, default=Decimal('0.001'))
    parser.add_argument('--resolution', type=Decimal, default=Decimal('0.001'))
    parser.add_argument('--max-period', type=Decimal, default=Decimal('1000'))
    parser.add_argument('--wave', choices=['fsdb', 'vcd'], default='fsdb')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--accept-unconverged', action='store_true')
    parser.add_argument('--input-delays-reviewed', action='store_true')
    options = parser.parse_args()
    if options.design == 'all':
        if options.action != 'all':
            parser.error('All designs currently require action all')
        if options.optimization is None:
            options.optimization = 'all'
    if options.jobs <= 0:
        parser.error('--jobs must be positive')

    if (options.max_attempts <= 0 or
            not options.resolution.is_finite() or options.resolution <= 0 or
            not options.max_period.is_finite() or options.max_period < Decimal('0.001') or
            not options.tolerance.is_finite() or options.tolerance < 0):
        parser.error('Search limits must be finite and positive; tolerance may be zero')
    if options.round is not None and options.round <= 0:
        parser.error('--round must be positive')
    if options.design != 'collect':
        if options.action is None:
            parser.error('Specify pre, synth, tune, post, all or select')
        if options.action != 'pre' and options.optimization is None:
            parser.error('Specify Area, Delay or Between')
        if options.optimization == 'all' and options.action != 'all':
            parser.error('Optimization all is only valid with action all')
        if options.action == 'select' and options.round is None:
            parser.error('select requires --round N')
        if options.action == 'synth' and (
                options.period is None or not options.period.is_finite() or
                not Decimal('0.001') <= options.period <= options.max_period):
            parser.error('synth requires --period NS within bounds')
    return options


def round_period(value, resolution):
    """helper function for tuning: round upward to a multiple of the period resolution."""
    return math.ceil(value / resolution) * resolution


@state_locked
def choose(meta):
    """helper function for synthesis selection: record a timing-passing round.

    Require complete synthesis and nonnegative slack. Keep the previous verified
    selection, mark a new candidate's post-sim pending, and persist the map.
    Reselecting the same round updates convergence without discarding verification.
    """
    require(meta['status'] == 'complete' and meta['metrics']['slack_ns'] >= 0,
            'Only complete timing-passing rounds can be selected')
    data = selected()
    dest = data['designs'].setdefault(meta['design'], {})
    old = dest.get(meta['optimization'], {})
    previous = old.get('last_verified')
    if (old.get('round') == meta['round'] and
            old.get('synthesis_id') == meta['synthesis_id'] and
            old.get('search_id') == meta.get('search_id') and
            old.get('timestamp') == meta.get('timestamp')):
        old.update(converged=meta.get('converged', False))
        atomic(ROOT / 'result/selected_runs.json', data)
        return old
    if old.get('post', {}).get('status') == 'passed':
        previous = old
    value = dict(meta)
    value['post'] = {'status': 'pending'}
    if previous:
        value['last_verified'] = {k: v for k, v in previous.items() if k != 'last_verified'}
    dest[meta['optimization']] = value
    atomic(ROOT / 'result/selected_runs.json', data)
    logging.info(f"Selected {meta['optimization']} round{meta['round']}; post-sim pending")
    return value


@state_locked
def mark_post(design, opt, record):
    """helper function for post-simulation: attach verification to the selection.

    Replaced post-sim directories invalidate the prior verification record.
    Set last_verified only when the replacement passes.
    """
    data = selected()
    value = data['designs'][design][opt]
    value['post'] = record
    value.pop('last_verified', None)
    if record['status'] == 'passed':
        value['last_verified'] = {k: v for k, v in value.items() if k != 'last_verified'}
    atomic(ROOT / 'result/selected_runs.json', data)
