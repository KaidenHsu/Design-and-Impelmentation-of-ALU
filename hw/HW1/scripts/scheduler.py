"""Bounded task scheduling, grouped output and child-process cancellation."""

import logging
import os
import queue
import re
import signal
import tempfile
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


class EventHandler(logging.Handler):
    """Route formatted logging records through the coordinator."""

    def emit(self, record):
        """helper function for logging: enqueue a worker record or print on main."""
        runtime.emit(self.format(record))


class Runtime:
    """Own worker output, active tools and the dependency-aware executor."""

    def __init__(self):
        """helper function for initialization: create thread-safe runtime state."""
        self.events = queue.Queue()
        self.context = threading.local()
        self.stopped = threading.Event()
        self.process_lock = threading.RLock()
        self.processes = {}
        self.verbose = False
        self.batch = False
        self.buffers = {}
        self.warnings = {}
        self.completions = {}

    def emit(self, text, raw=False):
        """helper function for output: only the coordinator writes to stdout."""
        key = getattr(self.context, 'key', None)
        if key is None:
            print(text, end='' if text.endswith('\n') else '\n', flush=True)
        else:
            self.events.put((key, text, raw))

    def check_cancel(self):
        """helper function for cancellation: stop before cleanup or a new tool."""
        if self.stopped.is_set():
            raise KeyboardInterrupt('Session interrupted')

    def register(self, process):
        """helper function for process ownership: record a newly launched child.

        The caller holds process_lock across the cancellation check, Popen and
        registration, so cancellation cannot miss a child being started.
        """
        self.processes[process.pid] = process

    def terminate(self, process, force=False):
        """helper function for cancellation: signal a tool's entire POSIX group.

        Windows termination is used only for portable local bookkeeping checks;
        the course server uses POSIX process groups, including tool descendants.
        """
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif process.poll() is None:
                process.kill() if force else process.terminate()
        except ProcessLookupError:
            pass

    def stop(self, force=False):
        """helper function for interruption: prevent launches and signal children."""
        self.stopped.set()
        with self.process_lock:
            for process in list(self.processes.values()):
                self.terminate(process, force)

    def drain(self):
        """helper function for output: log history and show progress only once.

        HW1_LOG_ONLY lines reach tee/master history but are hidden by the launcher.
        Only tool diagnostics are buffered until completion. Timestamped worker
        records are never replayed; verbose tool lines stream with task tags.
        """
        for unused in range(1000):
            try:
                key, text, raw = self.events.get_nowait()
            except queue.Empty:
                break
            tag = '/'.join(part for part in key if part)
            for line in text.splitlines() or ['']:
                print('HW1_LOG_ONLY:[{}] {}'.format(tag, line), flush=True)
                if raw:
                    if line.startswith('$ '):
                        continue
                    if self.verbose:
                        self.emit('HW1_TERMINAL:[{}] {}'.format(tag, line))
                        continue
                    warning = self.warnings.get(key, False)
                    if line.startswith(('Warning:', 'Error:')):
                        warning = True
                    elif not line or line.startswith(('Information:', 'INFO:', 'Using ')):
                        warning = False
                    # DC's diagnostic code terminates even an unindented warning.
                    self.warnings[key] = warning and not re.search(r'\([A-Z]+-\d+\)\s*$', line)
                    if not (warning or line.startswith(('WARN:', 'HW1_DC_ERROR:'))):
                        continue
                else:
                    self.warnings[key] = False
                    record = re.search(r'\[(INFO|WARN|ERROR|DONE)\] (.*)', line)
                    if record:
                        level, message = record.groups()
                        # Launch/completion are owned by the coordinator; omit
                        # duplicate simulation headings and routine path chatter.
                        if level == 'DONE':
                            self.completions[key] = message
                            continue
                        if not self.verbose and (
                                message.startswith(('cwd=', 'Artifact:'))):
                            continue
                        if message.startswith('=== '):
                            if 'attempt ' not in message:
                                continue
                            message = message.split(' | attempt ', 1)[1].rstrip(' =')
                            message = 'Attempt ' + message.replace(' | target ', ': target ')
                        logging.log({'INFO': logging.INFO, 'WARN': logging.WARNING,
                                     'ERROR': logging.ERROR}[level], '[%s] %s', tag, message)
                        continue
                    if not line:
                        continue
                if key not in self.buffers:
                    self.buffers[key] = tempfile.SpooledTemporaryFile(
                        max_size=1024 * 1024, mode='w+', encoding='utf-8')
                buffer = self.buffers[key]
                buffer.write(line + '\n')

    def replay(self, key):
        """helper function for diagnostics: print one intact task block.

        Repeated diagnostics from tuning attempts are collapsed for the terminal;
        every occurrence remains in the original tool logs and master history.
        """
        tag = '/'.join(part for part in key if part)
        buffer = self.buffers.pop(key, None)
        if buffer:
            buffer.seek(0)
            lines = []
            seen = set()
            diagnostic = []
            for line in buffer:
                if line.startswith(('Warning:', 'Error:', 'WARN:', 'HW1_DC_ERROR:')) and diagnostic:
                    text = ''.join(diagnostic)
                    if text not in seen:
                        lines.append(text)
                        seen.add(text)
                    diagnostic = []
                diagnostic.append(line)
            text = ''.join(diagnostic)
            if text and text not in seen:
                lines.append(text)
            if lines:
                self.emit('HW1_TERMINAL:')
                self.emit('HW1_TERMINAL:Diagnostics [{}] (repeated messages collapsed):'.format(tag))
                for text in lines:
                    for line in text.splitlines():
                        self.emit('HW1_TERMINAL:' + line)
            buffer.close()
        self.warnings.pop(key, None)

    def summary(self, tasks, states):
        """helper function for completion: group final statuses once per strategy."""
        self.emit('HW1_TERMINAL:')
        self.emit('HW1_TERMINAL:Execution summary')
        designs = list(OrderedDict((key[0], None) for key in tasks))
        for design in designs:
            if self.batch:
                self.emit('\n' + '=' * 40 + '\n' + design + '\n' + '=' * 40)
            for opt in ('', 'Area', 'Delay', 'Between'):
                keys = [key for key in tasks if key[:2] == (design, opt)]
                if not keys:
                    continue
                if opt:
                    self.emit('\n# ' + opt if self.batch else
                              '\n' + '=' * 40 + '\n' + opt + '\n' + '=' * 40)
                for key in keys:
                    state = states.get(key, {'status': 'interrupted'})
                    detail = ': ' + state['error'] if state.get('error') else ''
                    self.emit('HW1_TERMINAL:{}: {}{}'.format(key[2], state['status'], detail))

    def invoke(self, key, function):
        """helper function for worker execution: attach output context and errors."""
        self.context.key = key
        try:
            self.check_cancel()
            return function()
        except BaseException:
            self.emit(traceback.format_exc())
            raise
        finally:
            del self.context.key

    def run(self, tasks, jobs, batch=False):
        """Execute a DAG within one global job limit and keep independent work.

        Each task contains a function and dependency keys. Failed dependencies
        skip downstream work. Main-thread interruption cancels pending futures,
        terminates active process groups and joins workers before returning.
        """
        self.batch = batch
        pending = OrderedDict(tasks)
        active = {}
        states = {}
        executor = ThreadPoolExecutor(max_workers=jobs)
        interrupted = False
        try:
            while pending or active:
                self.drain()
                for key, task in list(pending.items()):
                    failed = [dep for dep in task['deps']
                              if states.get(dep, {}).get('status') in ('failed', 'skipped', 'interrupted')]
                    if failed:
                        reason = 'Dependency failed: ' + ', '.join('/'.join(x for x in dep if x) for dep in failed)
                        states[key] = {'status': 'skipped', 'error': reason}
                        logging.warning('[%s] Skipped: %s', '/'.join(x for x in key if x), reason)
                        del pending[key]
                ready = [key for key, task in pending.items()
                         if all(states.get(dep, {}).get('status') == 'passed' for dep in task['deps'])]
                # A ready Between search need not wait behind its parents' post-sims.
                if jobs > 1:
                    ready.sort(key=lambda key: key[1:] != ('Between', 'Synthesis'))
                for key in ready[:max(0, jobs - len(active))]:
                    task = pending.pop(key)
                    logging.info('[%s] Started', '/'.join(x for x in key if x))
                    active[executor.submit(self.invoke, key, task['function'])] = key
                if not active:
                    if pending and not ready:
                        # Newly skipped parents propagate on the next iteration.
                        if any(states.get(dep, {}).get('status') in ('failed', 'skipped', 'interrupted')
                               for task in pending.values() for dep in task['deps']):
                            continue
                        raise RuntimeError('Task dependencies cannot make progress')
                    continue
                done, unused = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    key = active.pop(future)
                    try:
                        future.result()
                        states[key] = {'status': 'passed'}
                    except BaseException as exc:
                        states[key] = {'status': 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                                       'error': '{}: {}'.format(type(exc).__name__, exc)}
                    while not self.events.empty():
                        self.drain()
                    self.replay(key)
                    level = 25 if states[key]['status'] == 'passed' else logging.ERROR
                    completion = self.completions.pop(key, None)
                    message = completion if states[key]['status'] == 'passed' and completion else states[key]['status']
                    logging.log(level, '[%s] %s', '/'.join(x for x in key if x), message)
                    if states[key].get('error'):
                        logging.error('[%s] %s', '/'.join(x for x in key if x), states[key]['error'])
        except KeyboardInterrupt:
            interrupted = True
            self.stop()
            for key in pending:
                states[key] = {'status': 'interrupted', 'error': 'Session interrupted before launch'}
            deadline = time.monotonic() + 10
            while active:
                self.drain()
                if time.monotonic() >= deadline:
                    self.stop(force=True)
                for future in list(active):
                    if future.cancel() or future.done():
                        key = active.pop(future)
                        if future.cancelled():
                            states[key] = {'status': 'interrupted', 'error': 'Session interrupted before launch'}
                        else:
                            try:
                                future.result()
                                states[key] = {'status': 'passed'}
                            except BaseException as exc:
                                states[key] = {'status': 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                                               'error': '{}: {}'.format(type(exc).__name__, exc)}
                        self.drain()
                        self.replay(key)
                if active:
                    wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
        finally:
            if active:
                # Unexpected coordinator errors must not strand tool processes.
                self.stop()
                deadline = time.monotonic() + 10
                remaining = {future for future in active if not future.done()}
                while remaining:
                    self.drain()
                    if time.monotonic() >= deadline:
                        self.stop(force=True)
                    wait(remaining, timeout=0.1, return_when=FIRST_COMPLETED)
                    remaining = {future for future in remaining if not future.done()}
            executor.shutdown(wait=True)
            while not self.events.empty():
                self.drain()
            for key in list(self.buffers):
                self.replay(key)
        self.summary(tasks, states)
        return states, interrupted


runtime = Runtime()
