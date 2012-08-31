import sys
import io
import pickle
from inspect import istraceback, isclass

from pulsar import is_failure, CLEAR_ERRORS, make_async, get_actor, async

from .utils import inject_async_assert


__all__ = ['TestRequest']


@async(max_errors=1, description='Test ')
def run_test_function(test, func, istest=False):
    '''Run function *func* which belong to *test*.

:parameter test: test instance or class
:parameter func: test function belonging to *test*
:parameter istest: flag indicating if this is a test or a setup/teardown
    function.
:return: an asynchronous result
'''
    if func is None:
        return func
    class_method = isclass(test)
    if istest:
        worker = get_actor()
        runner = worker.app.runner
        test = runner.getTest(test)
    test.istest = istest
    test_function = getattr(test, func.__name__)
    return test_function()

def test_method(cls, method):
    try:
        return cls(method)
    except ValueError:
        return None


class TestRequest(object):
    '''A class which wraps a test case class and runs all its test functions

.. attribute:: testcls

    A :class:`unittest.TestCase` class to be run on this request.

.. attribute:: tag

    A string indicating the tag associated with :attr:`testcls`.
'''
    def __init__(self, testcls, tag):
        self.testcls = testcls
        self.tag = tag

    def __repr__(self):
        return self.testcls.__name__
    __str__ = __repr__

    def start(self, worker):
        '''Run all test functions from the :attr:`testcls` using the
following algorithm:

* Run the class method ``setUpClass`` of :attr:`testcls` if defined.
* Call :meth:`run_test` for each test functions in :attr:`testcls`
* Run the class method ``tearDownClass`` of :attr:`testcls` if defined.
'''
        # Reset the runner
        worker.app.local.pop('runner')
        runner = worker.app.runner
        testcls = self.testcls
        testcls.tag = self.tag
        inject_async_assert(testcls)
        testcls.cfg = worker.cfg
        all_tests = runner.loadTestsFromTestCase(testcls)

        if all_tests.countTestCases():
            skip_tests = getattr(testcls, "__unittest_skip__", False)
            should_stop = False
            test_cls = test_method(testcls, 'setUpClass')
            if test_cls and not skip_tests:
                outcome = run_test_function(testcls,
                                            getattr(testcls,'setUpClass'))
                yield outcome
                should_stop = self.add_failure(test_cls, runner, outcome.result)

            if not should_stop:
                # Loop over all test cases in class
                for test in all_tests:
                    yield self.run_test(test, runner)

            test_cls = test_method(testcls, 'tearDownClass')
            if test_cls and not skip_tests:
                outcome = run_test_function(testcls,getattr(testcls,
                                                        'tearDownClass'))
                yield outcome
                self.add_failure(test_cls, runner, outcome.result)

            # Clear errors
            yield CLEAR_ERRORS

        # send runner result to monitor
        yield worker.send(worker.monitor, 'test_result', testcls.tag,
                          testcls, runner.result)

    def run_test(self, test, runner):
        '''\
Run a *test* function using the following algorithm

* Run :meth:`_pre_setup` method if available in :attr:`testcls`.
* Run :meth:`setUp` method in :attr:`testcls`.
* Run the test function.
* Run :meth:`tearDown` method in :attr:`testcls`.
* Run :meth:`_post_teardown` method if available in :attr:`testcls`.
'''
        try:
            success = True
            runner.startTest(test)
            testMethod = getattr(test, test._testMethodName)
            if (getattr(test.__class__, "__unittest_skip__", False) or
                getattr(testMethod, "__unittest_skip__", False)):
                reason = (getattr(test.__class__,
                                  '__unittest_skip_why__', '') or
                          getattr(testMethod,
                                  '__unittest_skip_why__', ''))
                runner.addSkip(test, reason)
                raise StopIteration()

            if hasattr(test,'_pre_setup'):
                outcome = run_test_function(test, test._pre_setup)
                yield outcome
                success = not self.add_failure(test, runner, outcome.result)

            if success:
                outcome = run_test_function(test, test.setUp)
                yield outcome
                if not self.add_failure(test, runner, outcome.result):
                    # Here we perform the actual test
                    outcome = run_test_function(test, testMethod, istest=True)
                    yield outcome
                    success = not self.add_failure(test, runner, outcome.result)
                    if success:
                        test.result = outcome.result
                    outcome = run_test_function(test, test.tearDown)
                    yield outcome
                    if self.add_failure(test, runner, outcome.result):
                        success = False
                else:
                    success = False

            if hasattr(test,'_post_teardown'):
                outcome = run_test_function(test,test._post_teardown)
                yield outcome
                if self.add_failure(test, runner, outcome.result):
                    success = False

            runner.stopTest(test)
        except StopIteration:
            success = False
        except Exception as e:
            self.add_failure(test, runner, e)
            success = False

        if success:
            runner.addSuccess(test)

    def add_failure(self, test, runner, failure):
        if is_failure(failure):
            for trace in failure:
                e = trace[1]
                try:
                    raise e
                except test.failureException:
                    runner.addFailure(test, trace)
                except:
                    runner.addError(test, trace)
                return True
        else:
            return False