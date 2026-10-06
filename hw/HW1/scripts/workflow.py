#!/usr/bin/env python3
"""HW1 synthesis, simulation and report workflow; launch through run.sh."""

import csv
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import time
import traceback
import uuid
from collections import OrderedDict
from decimal import Decimal
from functools import partial
from pathlib import Path

import helper
from scheduler import EventHandler, runtime
from helper import (ROOT, DESIGNS, OPTS, EXPORTS, DEFAULT_CELL_MODEL,
                    SESSION, START)


def synth(design, opt, period, search=None, attempt=1):
    """Run one isolated DC synthesis attempt at the requested period in ns.

    Invoke dcnxt_shell -f dc.tcl inside the round, retain outputs under that round,
    validate metrics and exports, and record artifact hashes. Return completed
    metadata; record failed/interrupted status before propagating an exception.
    Logs and artifact rounds use search-local attempt numbers.
    A standalone synthesis clears this strategy's old logs before its attempt.
    """
    if not shutil.which('dcnxt_shell'):
        raise RuntimeError("Missing " + 'dcnxt_shell' + "; initialize the course server environment first")
    files = helper.rtl_files(design)
    identity = helper.synthesis_id(design, args)
    if search is None:
        helper.reset_synthesis(design, opt)
    n = attempt
    out = ROOT / 'gate_level' / design / opt / f'round{n}'
    helper.prepare_directory(out)
    shutil.copyfile(ROOT / 'compile/dc.tcl', out / 'dc.tcl')
    (out / 'work').mkdir()
    (out / 'rtl_files.txt').write_text('\n'.join(map(str, files)) + '\n')
    log = helper.prepare_log(ROOT / 'log' / design / opt / f'synth{attempt}.log')
    meta = {
        'schema': 1, 'design': design, 'optimization': opt, 'round': n,
        'path': str(out.relative_to(ROOT)), 'log': str(log.relative_to(ROOT)), 'status': 'running',
        'synthesis_id': identity, 'period_ns': str(period), 'search_id': search,
        'attempt': attempt,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    record_path = out / 'metadata.json'
    helper.atomic(record_path, meta)
    env = {
        'HW1_DESIGN': design, 'HW1_OPT': opt, 'HW1_PERIOD': str(period),
        'HW1_ROUND': str(n), 'HW1_OUT': str(out),
        'HW1_RTL_LIST': str(out / 'rtl_files.txt'),
        'HW1_TIMING_REVIEWED': '1' if args.input_delays_reviewed else '0',
    }
    helper.phase(f'Synthesis | {design} | {opt} | attempt {attempt} | target {period} ns')
    logging.info(f'Artifact: gate_level/{design}/{opt}/round{n}')
    try:
        helper.run_command(['dcnxt_shell', '-f', 'dc.tcl'], out, log, env)
        values = helper.metrics(out / 'metrics.tsv')
        helper.require(abs(values['period_ns'] - float(period)) <= 1e-06, 'DC used a different period')
        for name in EXPORTS:
            helper.require((out / name).is_file() and (out / name).stat().st_size > 0, f'Missing export {name}')
        hashes = {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in EXPORTS}
        meta = helper.preserve_meta(record_path, status='complete', metrics=values, artifact_hashes=hashes)
        logging.info(f"Attempt {attempt}: slack={values['slack_ns']:.6g} ns, area={values['total_um2']:.6g} um2")
        return meta
    except (Exception, KeyboardInterrupt) as exc:
        helper.preserve_meta(record_path, status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed', error=str(exc))
        raise


def tune(design, opt):
    """Iteratively synthesize and select a converged timing-passing candidate.

    Area starts relaxed; Delay starts aggressive; Between starts at the selected
    Area/Delay midpoint and never searches below it. Adjust period by subtracting
    slack, round upward to resolution, and use a passing/failing bracket when
    needed. Converge on nonnegative slack within tolerance, a passing Between
    midpoint, or a bracket within resolution. Preserve every attempt and any
    passing fallback; raise if the search stops without convergence.
    """
    # Reserve an attempt before starting, including interrupted/failed attempts in the budget.
    identity = helper.search_config(design, opt, args)
    if opt == 'Between':
        a, d = (helper.entry(design, 'Area'), helper.entry(design, 'Delay'))
        for value in [a, d]:
            helper.require(value.get('converged') and value.get('synthesis_id') == identity['identity'],
                    'Between needs current converged Area and Delay; tune them first')
        area_period = Decimal(str(a['metrics']['period_ns']))
        delay_period = Decimal(str(d['metrics']['period_ns']))
        helper.require(area_period >= delay_period, 'Area period below Delay; review selected runs')
        floor = (area_period + delay_period) / 2
        identity['dependencies'] = [a.get('search_id') or a['timestamp'],
                                    d.get('search_id') or d['timestamp']]
    else:
        floor = Decimal('0.001')
    directory = ROOT / 'gate_level' / design / opt
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / 'search.json'
    state = helper.load(state_path)
    if args.resume:
        helper.require(state and state['configuration'] == identity,
                'Cannot resume: search inputs changed; start a fresh tune')
        if state.get('finished'):
            helper.require(state.get('converged'), 'Search exhausted; start a fresh search with changed limits')
            candidate = helper.load(ROOT / state['best'] / 'metadata.json')
            candidate['converged'] = True
            helper.choose(candidate)
            return candidate
    else:
        if opt == 'Between':
            seed = floor
        elif opt == 'Area':
            seed = Decimal(os.environ.get('HW1_AREA_SEED_NS', '10'))
        else:
            seed = Decimal(os.environ.get('HW1_DELAY_SEED_NS', '0.01'))
        helper.reset_synthesis(design, opt)
        state = {
            'configuration': identity, 'id': SESSION + '-' + opt,
            'attempts': [], 'next_period': str(seed), 'finished': False, 'best': None,
        }


    quant = partial(helper.round_period, resolution=args.resolution)
    candidate = None
    converged = False
    while len(state['attempts']) < args.max_attempts:
        runtime.check_cancel()
        p = quant(max(floor, Decimal(state['next_period'])))
        if p > args.max_period:
            logging.warning(f'Search period bound reached: {p} ns')
            break
        old = [m for m in state['attempts'] if m.get('status') == 'complete']
        seen = {Decimal(m['period_ns']) for m in old}
        if p in seen:
            logging.warning('Repeated candidate; stopping search')
            break
        state['attempts'].append({'period_ns': str(p), 'status': 'running'})
        helper.atomic(state_path, state)
        try:
            result = synth(design, opt, p, state['id'], attempt=len(state['attempts']))
        except (Exception, KeyboardInterrupt) as exc:
            state['attempts'][-1]['status'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
            helper.atomic(state_path, state)
            raise
        state['attempts'][-1] = result
        good = [m for m in old + [result] if m['metrics']['slack_ns'] >= 0]
        if good:
            candidate = min(good, key=lambda m: m['metrics']['period_ns'])
            state['best'] = candidate['path']
        slack = Decimal(str(result['metrics']['slack_ns']))
        passing = slack >= 0
        if passing and (slack <= args.tolerance or (opt == 'Between' and p == quant(floor))):
            candidate = result
            converged = True
            break
        failed = [Decimal(m['period_ns']) for m in old + [result]
                  if m['metrics']['slack_ns'] < 0 and
                  (candidate is None or Decimal(m['period_ns']) < Decimal(candidate['period_ns']))]
        low = max(failed, default=None)
        high = Decimal(candidate['period_ns']) if candidate else None
        if low is not None and high is not None and high - low <= args.resolution:
            converged = True
            break
        nxt = max(floor, p - slack)
        if low is not None and high is not None:
            if not low < nxt < high or quant(nxt) in seen or quant(nxt) == p:
                nxt = (low + high) / 2
        nxt = quant(nxt)
        if nxt in seen or nxt == p:
            nxt = p + args.resolution if not passing else max(quant(floor), p - args.resolution)
        if nxt in seen:
            logging.warning('Search cannot make progress at configured resolution')
            break
        logging.info('Failing period: %s; passing period: %s',
                     str(low) + ' ns' if low is not None else 'not found',
                     str(high) + ' ns' if high is not None else 'not found')
        logging.info(f'Next period: {nxt} ns (current period minus slack {slack} ns)')
        state['next_period'] = str(nxt)
        helper.atomic(state_path, state)
    state['finished'] = True
    state['converged'] = converged
    helper.atomic(state_path, state)
    if candidate:
        candidate = helper.preserve_meta(ROOT / candidate['path'] / 'metadata.json', converged=converged)
        helper.choose(candidate)
    helper.require(converged, 'Search did not converge; passing fallback retained. Review logs/limits.')
    return candidate


def simulation(design, post=False, opt=None, forced=None):
    """Compile and run the TA-based pre- or post-synthesis testbench with VCS.

    Generate run_config.vh in a replaced simulation directory. Pre-simulation uses RTL;
    post-simulation requires the selected matching netlist, SDF and cell model.
    Require 100 successful comparisons and a nonempty waveform, plus completed SDF
    annotation with no errors/unaccepted warnings. Save the verification record;
    preserve failure/interruption evidence and reuse matching passes on resume.
    """
    # Generated header is an explicit compiler input; file/SDF paths are absolute and escaped for Verilog.
    helper.phase(f'Post-sim | {design} | {opt}' if post
                 else f'Pre-sim | {design}')
    if not shutil.which('vcs'):
        raise RuntimeError("Missing " + 'vcs' + "; initialize the course server environment first")
    stages = DESIGNS[design][1]
    files = helper.rtl_files(design)
    folder = 'post_sim' if post else 'pre_sim'
    helper.validate_vectors(folder)
    candidate = forced or (helper.entry(design, opt) if post else None)
    if post:
        helper.require(candidate and candidate.get('synthesis_id') == helper.synthesis_id(design, args),
                'Selected netlist missing/stale; rerun synthesis')
        helper.require(helper.artifacts_current(candidate), 'Selected artifacts changed/missing; rerun synthesis')
        helper.require(candidate.get('converged') or args.accept_unconverged,
                'Selected fallback is unconverged; use --accept-unconverged to inspect it')
        # The original TA checker uses a fixed 5 ns cycle, independently of DC.
        # A pass here verifies that schedule, not the tuned synthesis frequency.
        period = Decimal('5.0')
    else:
        period = Decimal(os.environ.get('HW1_PRE_CYCLE_NS', '5'))
    if post:
        logging.info('TA verification cycle: %s ns; synthesis target: %s ns',
                     period, candidate['metrics']['period_ns'])
    helper.require(period >= Decimal('0.001'), 'Simulation cycle must be >=0.001 ns')
    prefix = 'post_sim' if post else 'pre_sim'
    out = ROOT / folder / design / (opt if post else '')
    log = ROOT / 'log' / design / opt / 'post_sim.log' if post else ROOT / 'log' / design / 'pre_sim.log'
    vid = helper.verification_id(design, folder, period, args)
    previous = candidate.get('post', {}) if post else {}
    if args.resume and previous.get('status') == 'passed' and previous.get('verification_id') == vid:
        logging.info(f'Reusing matching passed {design} {opt} post-sim')
        return candidate['post']

    if not post:
        for old_log in (ROOT / 'log' / design).glob('pre_sim*.log'):
            if re.fullmatch(r'pre_sim\d+\.log', old_log.name):
                helper.remove_output(old_log)
    runtime.check_cancel()
    helper.prepare_directory(out, replace=True)
    helper.prepare_log(log)
    record = {
        'schema': 1, 'status': 'running', 'design': design, 'verification_id': vid,
        'synthesis_id': helper.synthesis_id(design, args), 'schedule': 'TA_original',
        'pipeline_stages': stages, 'period_ns': str(period),
        'path': str(out.relative_to(ROOT)), 'log': str(log.relative_to(ROOT)),
        'round': candidate['round'] if post else None,
    }
    if post:
        record['synthesis_period_ns'] = str(candidate['metrics']['period_ns'])
    record_path = out / 'verification.json'
    helper.atomic(record_path, record)
    if post:
        helper.mark_post(design, opt, record)
    else:
        helper.atomic(out / 'latest.json', record)

    header = ['`define ' + design, '`define CYCLE ' + str(period),
              '`define FILE_A ' + json.dumps(str(ROOT / folder / 'a.txt')),
              '`define FILE_B ' + json.dumps(str(ROOT / folder / 'b.txt'))]
    if post:
        cell = Path(os.environ.get('HW1_CELL_MODEL', DEFAULT_CELL_MODEL))
        helper.require(cell.is_file(), f'Standard cell model missing: {cell}')
        sdf = ROOT / candidate['path'] / 'timing.sdf'
        header += ['`define SDF_FILE ' + json.dumps(str(sdf))]
        inputs = [ROOT / candidate['path'] / 'netlist.v', cell]
    else:
        inputs = files
    if args.wave == 'fsdb':
        header += ['`define HW1_FSDB']
    (out / 'run_config.vh').write_text('\n'.join(header) + '\n')
    # Retain the TA options; build paths and testbench language are setup additions.
    command = ['vcs', '-R', '-debug_access+all', '+full64', '+access+r', '-sverilog',
               '-top', 'testbench', '-o', str(out / 'simv'), '+incdir+' + str(out)]
    if post:
        command += ['-error=noMPD', '+neg_tchk']
    else:
        command += ['+v2k']
        command += ['+incdir+' + str(p) for p in sorted({f.parent for f in files})]
    if args.wave == 'fsdb':
        command += ['+vcs+fsdbon', '+fsdb+mda', '+fsdbfile+wave.fsdb']
        command += shlex.split(os.environ.get('HW1_VCS_FSDB_FLAGS', ''))
    command += [str(ROOT / folder / 'testbench.v')] + list(map(str, inputs))
    try:
        helper.run_command(command, out, log)
        result = helper.load(out / 'tb_result.json')
        helper.require(result and result.get('checked') == 100 and result.get('errors') == 0
                and result.get('status') == 'passed',
                'Testbench completion missing or comparisons failed')
        text = log.read_text(errors='replace')
        if post:
            helper.require('HW1_SDF_ANNOTATION_REQUESTED' in text, 'SDF annotation was not requested')
            annotation = out / 'sdf_annotation.log'
            helper.require(annotation.is_file(), 'SDF annotation log absent; annotation success is unconfirmed')
            warning_counts = helper.validate_sdf(annotation, text)
        wave = out / ('wave.fsdb' if args.wave == 'fsdb' else 'wave.vcd')
        helper.require(wave.exists() and wave.stat().st_size > 0,
                'Waveform missing; check FSDB integration or retry with --wave vcd')
        record = helper.preserve_meta(record_path, status='passed', checked=100, errors=0,
                               waveform=str(wave.relative_to(ROOT)), wave_format=args.wave,
                               **({'sdf_warnings': warning_counts} if post else {}))
        if post:
            helper.mark_post(design, opt, record)
        else:
            helper.atomic(out / 'latest.json', record)
        logging.log(25, f"{design} {prefix.replace('_', '-')} passed; waveform {str(wave.relative_to(ROOT))}")
        return record
    except (Exception, KeyboardInterrupt) as exc:
        status = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
        record = helper.preserve_meta(record_path, status=status, error=str(exc))
        if post:
            helper.mark_post(design, opt, record)
        else:
            helper.atomic(out / 'latest.json', record)
        raise


def collect():
    """Rebuild report CSVs and selection provenance without invoking EDA tools.

    Check source/artifact fingerprints and recorded pre/post verification.
    Collect timing, area, power and stage measurements; leave stale values blank
    and warn about provisional or incomplete results. Replace the report CSVs,
    then save their hashes and collection generation in selected_runs.json.
    Report latency follows the TA convention of delay times declared stages.
    """
    helper.phase('Result collection')
    data = helper.selected()
    generation = uuid.uuid4().hex[:12]
    rows = []
    stage_rows = []
    for design in DESIGNS:
        for opt in OPTS:
            value = data['designs'].get(design, {}).get(opt)
            row = {'design': design, 'optimization': opt}
            if value:
                m = dict(value['metrics'])
                round_dir = ROOT / value['path']
                try:
                    m['delay_ns'] = helper.timing_report(round_dir / 'timing_report.txt')['delay_ns']
                    m['delay_basis'] = 'timing_report_data_arrival'
                except (OSError, RuntimeError) as exc:
                    logging.warning('%s %s: %s', design, opt, exc)
                    m['delay_ns'] = None
                stale = (value['synthesis_id'] != helper.synthesis_id(design, args)
                         or not helper.artifacts_current(value))
                post = value.get('post', {})
                pre = helper.load(ROOT / 'pre_sim' / design / 'latest.json', {})
                pre_status = helper.verification_status(pre, design, 'pre_sim', pre.get('period_ns', 5), args)
                post_status = helper.verification_status(post, design, 'post_sim', '5.0', args)
                pre_current = pre_status == 'passed'
                verification_current = post_status == 'passed'
                if stale:
                    status = 'stale'
                elif verification_current and pre_current:
                    status = 'verified'
                elif verification_current:
                    status = 'post_verified_pre_pending'
                elif post_status == 'pending':
                    status = 'verification_pending'
                elif post_status == 'stale':
                    status = 'verification_stale'
                else:
                    status = 'verification_failed'
                if not value.get('converged') and (not stale):
                    status = 'unconverged_' + status
                complete = (m['delay_ns'] is not None and m.get('coverage_status') == 'clean' and
                            all(m.get(key) is not None
                                for key in ('dynamic_w', 'leakage_w', 'power_w')))
                stageinfo = []
                stages = DESIGNS[design][1]
                for num in range(1, stages + 1) if stages > 1 else []:
                    report = round_dir / f'timing_report_stage{num}.txt'
                    try:
                        measured = helper.timing_report(report)
                        state = 'measured'
                    except (OSError, RuntimeError) as exc:
                        logging.warning('%s %s stage %s: %s', design, opt, num, exc)
                        measured = {'delay_ns': '', 'startpoint': '', 'endpoint': ''}
                        state = 'invalid'
                    stageinfo.append(dict(measured, generation=generation,
                                          design=design, optimization=opt,
                                          round=value['round'], stage=num, status=state,
                                          report=str(report.relative_to(ROOT))))
                if stages > 1 and (len(stageinfo) != stages or
                                   any(s['status'] != 'measured' for s in stageinfo)):
                    complete = False
                measured = [float(s['delay_ns']) for s in stageinfo if s['delay_ns']]
                for s in stageinfo:
                    s['critical'] = bool(s['delay_ns'] and abs(float(s['delay_ns']) - max(measured)) < 1e-09)
                stage_rows += stageinfo
                row.update({key: m[key] for key in
                            ('delay_ns', 'comb_um2', 'seq_um2', 'total_um2',
                             'dynamic_w', 'leakage_w', 'power_w')})
                row['latency_ns'] = m['delay_ns'] * stages if m['delay_ns'] is not None else ''
                value['report_status'] = {
                    'status': status,
                    'complete': complete,
                    'pre_status': pre_status,
                    'post_status': post_status,
                    'generation': generation,
                }
                value['pipeline_stage_results'] = [dict(stage) for stage in stageinfo]

                if stale:
                    for key in ['comb_um2', 'seq_um2', 'total_um2', 'delay_ns',
                                'latency_ns', 'dynamic_w', 'leakage_w', 'power_w']:
                        row[key] = ''
                    for stage in stageinfo:
                        stage['delay_ns'] = ''
                        stage['critical'] = ''

                if status != 'verified' or not complete:
                    logging.warning(f'{design} {opt}: {status}; report_complete={complete}. '
                        'Review selected_runs.json before using these measurements in the report.')
            rows.append(row)

    headers = [
        'design', 'optimization', 'comb_um2', 'seq_um2', 'total_um2',
        'delay_ns', 'latency_ns', 'dynamic_w', 'leakage_w', 'power_w',
    ]
    sh = ['design', 'optimization', 'stage', 'delay_ns', 'critical']
    table_hashes = {}

    for name, fields, content in [('summary.csv', headers, rows), ('pipeline_delays.csv', sh, stage_rows)]:
        p = ROOT / 'result' / name
        tmp = p.with_name(p.name + '.tmp.' + SESSION)
        with tmp.open('w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for row in content:
                report_row = {key: helper.report_number(key, row.get(key, '')) for key in fields}
                if name == 'pipeline_delays.csv' and not report_row['delay_ns']:
                    report_row['critical'] = ''
                w.writerow(report_row)
        os.replace(tmp, p)
        table_hashes[name] = hashlib.sha256((p).read_bytes()).hexdigest()

    data['collection_generation'] = generation
    data['report_table_hashes'] = table_hashes
    helper.atomic(ROOT / 'result/selected_runs.json', data)
    logging.log(25, f"Results refreshed: {ROOT / 'result'}; generation={generation}")


def pre_sim(design):
    """Run the shared pre-sim or reuse a matching pass for an aggregated resume."""
    if args.resume:
        helper.phase(f'Pre-sim | {design} | resume')
        previous = helper.load(ROOT / 'pre_sim' / design / 'latest.json', {})
        period = os.environ.get('HW1_PRE_CYCLE_NS', '5')
        helper.require(helper.verification_status(previous, design, 'pre_sim', period, args) == 'passed',
                       'Cannot resume all: run pre-simulation with the current inputs first')
        logging.info('Reusing matching passed pre-sim')
        return previous
    return simulation(design)


def aggregated():
    """Build and execute pre-sim, tuning and post-sim dependencies for the batch.

    One pre-sim owns each design. Area/Delay searches can overlap; Between waits
    for their tuning only. Keep unrelated tasks after failure and save statuses
    before raising. The coordinator joins workers before main collects results.
    """
    designs = [args.design]
    if args.design == 'all':
        implemented = []
        for name in DESIGNS:
            directory = ROOT / 'RTL' / name
            if ((directory / 'files.txt').is_file() or
                    any(directory.rglob('*.v')) or any(directory.rglob('*.sv'))):
                implemented.append(name)
            else:
                logging.warning('Skipping %s: RTL is not implemented', name)
        helper.require(implemented, 'No implemented designs found under RTL/')
        designs = implemented
    tasks = OrderedDict()
    optimizations = OPTS if args.optimization == 'all' else (args.optimization,)
    for design in designs:
        pre = (design, '', 'Pre-sim')
        tasks[pre] = {'function': partial(pre_sim, design), 'deps': []}
        for opt in optimizations:
            synthesis = (design, opt, 'Synthesis')
            dependencies = [pre]
            if opt == 'Between' and args.optimization == 'all':
                dependencies += [(design, parent, 'Synthesis') for parent in ('Area', 'Delay')]
            tasks[synthesis] = {'function': partial(tune, design, opt), 'deps': dependencies}
            tasks[(design, opt, 'Post-sim')] = {
                'function': partial(simulation, design, True, opt), 'deps': [synthesis]}
    logging.info('Scheduling %d tasks; jobs=%d; independent work continues after failures', len(tasks), args.jobs)
    states, interrupted = runtime.run(tasks, args.jobs, batch=args.design == 'all')
    helper.atomic(ROOT / 'log/session.json', {
        'session': SESSION, 'jobs': args.jobs,
        'tasks': [dict(design=key[0], optimization=key[1], phase=key[2], **value)
                  for key, value in states.items()],
    })
    if interrupted:
        raise KeyboardInterrupt('Session interrupted; task statuses saved in log/session.json')
    helper.require(all(value['status'] == 'passed' for value in states.values()),
                   'Some tasks failed or were skipped; see log/session.json and task logs')


def dispatch():
    """Dispatch a single action or the bounded aggregated task scheduler."""
    design, action, opt = args.design, args.action, args.optimization
    if design != 'collect':
        for name in DESIGNS if design == 'all' else (design,):
            helper.cleanup_pre_logs(name)
    if design == 'collect':
        collect()
    elif action == 'pre':
        simulation(design)
    elif action == 'synth':
        synth(design, opt, args.period)
    elif action == 'tune':
        tune(design, opt)
    elif action == 'select':
        meta = helper.round_metadata(design, opt, args.round, args)
        helper.require(meta.get('converged') or args.accept_unconverged,
                'Unconverged selection needs --accept-unconverged')
        helper.choose(meta)
    elif action == 'post':
        if args.round:
            helper.choose(helper.round_metadata(design, opt, args.round, args))
        simulation(design, True, opt)
    elif action == 'all':
        aggregated()


def main():
    """Run the CLI, report failures and refresh completed results before exiting."""
    global args
    args = helper.parse_args()
    logging.addLevelName(25, 'DONE')
    logging.addLevelName(logging.WARNING, 'WARN')
    runtime.verbose = args.verbose
    handler = EventHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                                           datefmt='%Y-%m-%dT%H:%M:%S%z'))
    logging.getLogger().handlers = [handler]
    logging.getLogger().setLevel(logging.INFO)
    signal.signal(signal.SIGTERM, helper.interrupted)
    signal.signal(signal.SIGINT, helper.interrupted)
    logging.info(f'Session={SESSION}; root={ROOT}; launch={Path.cwd()}')
    exitcode = 0
    try:
        dispatch()
    except (Exception, KeyboardInterrupt) as exc:
        exitcode = 130 if isinstance(exc, KeyboardInterrupt) else 1
        logging.error(f'{type(exc).__name__}: {exc}')
        traceback.print_exc()
        logging.info('Artifacts retained; use --resume only with unchanged inputs/settings.')
    finally:
        if args.design != 'collect':
            try:
                collect()
            except Exception as exc:
                logging.error(f'Collection failed: {exc}')
                exitcode = exitcode or 1
        logging.log(25 if exitcode == 0 else logging.ERROR,
                    'Session %s: exit=%d; elapsed=%.1fs; master log=%s',
                    SESSION, exitcode, time.time() - START, ROOT / 'scripts/run.log')
    return exitcode


if __name__ == '__main__':
    sys.exit(main())
