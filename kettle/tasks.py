from datetime import datetime
from threading import Thread
from time import sleep
import sys
import traceback

import logbook
from logbook import FileHandler, NestedSetup
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship, backref

from db import Base, session
from db.fields import JSONEncodedDict

from log_utils import get_thread_handlers, inner_thread_nested_setup, log_filename

def action_fn(action):
    def do_action(instance):
        action_start_dt = getattr(instance, '%s_start_dt' % (action,), None)
        if not action_start_dt:
            instance.call_and_record_action(action)
        else:
            raise Exception('%s already started at %s' % 
                    (action, action_start_dt))
    return do_action

def thread_wait(thread, abort_event):
    try:
        while True:
            if abort_event.is_set():
                pass # TODO: Set some kinda timeout
            if thread.is_alive():
                thread.join(1)
            else:
                break
    except Exception:
        # TODO: Fix logging
        print traceback.format_exc()
        logbook.exception()
        abort_event.set()


class FailingThread(Thread):
    def __init__(self, *args, **kwargs):
        super(FailingThread, self).__init__(*args, **kwargs)
        self.exc_info = None

    def run(self):
        try:
            super(FailingThread, self).run()
        except Exception:
            self.exc_info = sys.exc_info()


class Task(Base):
    __tablename__ = 'task'
    id = Column(Integer, primary_key=True)
    type = Column(String(50), nullable=False)
    rollout_id = Column(Integer, ForeignKey('rollout.id'))
    parent_id = Column(Integer, ForeignKey('task.id'))
    state = Column(JSONEncodedDict(1000))

    run_start_dt = Column(DateTime)
    run_error = Column(String(500))
    run_error_dt = Column(DateTime)
    run_return = Column(String(500))
    run_return_dt = Column(DateTime)
    run_traceback = Column(String(1000))

    rollback_start_dt = Column(DateTime)
    rollback_error = Column(String(500))
    rollback_error_dt = Column(DateTime)
    rollback_return = Column(String(500))
    rollback_return_dt = Column(DateTime)
    rollback_traceback = Column(String(1000))

    rollout = relationship('Rollout', backref=backref('tasks', order_by=id))
    children = relationship('Task', backref=backref('parent', remote_side='Task.id', order_by=id))


    @declared_attr
    def __mapper_args__(cls):
        # Implementing single table inheritance
        if cls.__name__ == 'Task':
            return {'polymorphic_on': cls.type,}
        else:
            return {'polymorphic_identity': cls.__name__}

    def __init__(self, rollout_id, *args, **kwargs):
        self.rollout_id = rollout_id
        self.state = {}
        self._init(*args, **kwargs)

    run = action_fn('run')
    __rollback = action_fn('rollback')

    def rollback(self):
        if not self.run_start_dt:
            raise Exception('Cannot rollback before running')
        self.__rollback()

    def call_and_record_action(self, action):
        setattr(self, '%s_start_dt' % (action,), datetime.now())
        self.save()
        # action_fn should be a classmethod, hence getattr explicitly on type
        action_fn = getattr(type(self), '_%s' % (action,))
        try:
            with self.log_setup_action(action):
                action_return = action_fn(self.state, self.children)
        except Exception, e:
            setattr(self, '%s_error' % (action,), e.message)
            setattr(self, '%s_traceback' % (action,), repr(traceback.format_exc()))
            setattr(self, '%s_error_dt' % (action,), datetime.now())
            raise
        else:
            setattr(self, '%s_return' % (action,), action_return)
            setattr(self, '%s_return_dt' % (action,), datetime.now())
        finally:
            self.save()

    def log_setup_action(self, action):
        return NestedSetup(
                get_thread_handlers() +
                (FileHandler(log_filename(
                    self.rollout_id, self.id, action), bubble=True),))

    def save(self):
        if self not in session.Session:
            session.Session.add(self)
        session.Session.commit()

    def _init(self, *args, **kwargs):
        pass

    @classmethod
    def _run(cls, state, children):
        pass

    @classmethod
    def _rollback(cls, state, children):
        pass

    def __repr__(self):
        return '<%s: id=%s, rollout_id=%s, state=%s>' % (
                self.__class__.__name__, self.id, self.rollout_id, repr(self.state))

    def friendly_str(self):
        return repr(self)

    def friendly_html(self):
        return self.friendly_str()

    def status(self):
        if not self.rollout_start_dt:
            return 'not_started'
        else:
            if not self.rollback_start_dt:
                if not self.rollout_finish_dt:
                    return 'started'
                else:
                    return 'finished'
            else:
                if not self.rollback_finish_dt:
                    return 'rolling_back'
                else:
                    return 'rolled_back'

    def run_threaded(self, abort_event):
        outer_handlers = get_thread_handlers()
        task_id = self.id
        def thread_wrapped_task():
            with inner_thread_nested_setup(outer_handlers):
                try:
                    # Reload from db
                    task = session.Session.query(Task).filter_by(id=task_id).one()
                    task.run()
                except Exception:
                    # TODO: Fix logging
                    print traceback.format_exc()
                    logbook.exception()
                    abort_event.set()
        thread = FailingThread(target=thread_wrapped_task, name=self.__class__.__name__)
        thread.start()
        return thread


class ExecTask(Task):
    desc_string = ''

    def _init(self, children, *args, **kwargs):
        self.save()

        for child in children:
            child.parent = self
            child.save()

    @classmethod
    def _run(cls, state, children):
        cls.execute(state, children)

    @classmethod
    def _rollback(cls, state, children):
        pass

    @staticmethod
    def get_abort_signal(tasks):
        from kettle.rollout import Rollout
        rollout_id, = set(task.rollout_id for task in tasks)
        abort = Rollout.abort_signals.get(rollout_id)
        return abort

    def friendly_str(self):
        return '%s%s' %\
                (type(self).desc_string, ''.join(['\n\t%s' % child.friendly_str() for child in self.children]))

    def friendly_html(self):
        return '%s<ul>%s</ul>' %\
                (type(self).desc_string,
                ''.join(['<li>%s</li>' % child.friendly_html() for child in self.children]))


class SequentialExecTask(ExecTask):
    def _init(self, children, *args, **kwargs):
        super(SequentialExecTask, self)._init(children, *args, **kwargs)
        self.state['task_order'] = [child.id for child in children]

    @classmethod
    def execute(cls, state, tasks):
        abort = cls.get_abort_signal(tasks)
        task_ids = {task.id: task for task in tasks}
        for task_id in state['task_order']:
            if abort.is_set():
                break
            task = task_ids.pop(task_id)
            thread = task.run_threaded(abort)
            thread_wait(thread, abort)
            if thread.exc_info is not None:
                raise Exception('Caught exception while executing task %s: %s' %
                        (task, thread.exc_info))
        else:
            assert not task_ids, "SequentialExecTask has children that are not\
                    in its task_order: %s" % (task_ids,)


class ParallelExecTask(ExecTask):
    desc_string = 'Execute in parallel:'

    @classmethod
    def execute(cls, state, tasks):
        threads = []
        abort = cls.get_abort_signal(tasks)
        for task in tasks:
            thread = task.run_threaded(abort)
            threads.append(thread)
        for thread in threads:
            thread_wait(thread, abort)
            if thread.exc_info is not None:
                raise Exception('Caught exception while executing task %s: %s' %
                        (thread.target, thread.exc_info))


class DelayTask(Task):
    def _init(self, minutes, reversible=False):
        self.state.update(
                minutes=minutes,
                reversible=reversible)

    @classmethod
    def _run(cls, state, children):
        cls.wait(mins=state['minutes'])

    @classmethod
    def _rollback(cls, state, children):
        if state['reversible']:
            cls.wait(mins=state['minutes'])

    @staticmethod
    def wait(mins):
        logbook.info('Waiting for %sm' % (mins,),)
        sleep(mins*60)

    def friendly_str(self):
        rev_str = ' (reversible)' if self.state['reversible'] else ''
        return 'Delay for {minutes} mins{rev_str}'.format(rev_str=rev_str, **self.state)
