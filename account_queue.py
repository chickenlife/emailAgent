"""Bounded fair scheduling across mailboxes with one shared Ollama worker.

Jobs retain their mailbox key; repeated work cannot impersonate another user.
The queue uses FIFO order within each mailbox and round-robin across mailboxes.
"""
import queue
import threading
from collections import deque
from time import monotonic


class AccountFairQueue:
    def __init__(self):
        self._condition = threading.Condition()
        self._accounts = deque()
        self._jobs = {}
        self._unfinished = 0

    def put(self, item):
        account, _ = item
        if not isinstance(account, str) or not account:
            raise ValueError('Job must include an account')
        with self._condition:
            jobs = self._jobs.get(account)
            if jobs is None:
                jobs = deque()
                self._jobs[account] = jobs
                self._accounts.append(account)
            jobs.append(item)
            self._unfinished += 1
            self._condition.notify()

    def get(self, timeout=None):
        end = None if timeout is None else monotonic() + timeout
        with self._condition:
            while not self._accounts:
                if timeout is None:
                    self._condition.wait()
                else:
                    remaining = end - monotonic()
                    if remaining <= 0:
                        raise queue.Empty
                    self._condition.wait(remaining)
            account = self._accounts.popleft()
            jobs = self._jobs[account]
            item = jobs.popleft()
            if jobs:
                self._accounts.append(account)
            else:
                del self._jobs[account]
            return item

    def task_done(self):
        with self._condition:
            if not self._unfinished:
                raise ValueError('task_done() called too many times')
            self._unfinished -= 1
            if self._unfinished == 0:
                self._condition.notify_all()

    def empty(self):
        with self._condition:
            return not self._accounts

    def qsize(self):
        with self._condition:
            return sum(len(jobs) for jobs in self._jobs.values())
