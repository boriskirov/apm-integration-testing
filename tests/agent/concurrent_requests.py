from datetime import datetime, timedelta
import copy
import os
import logging
import time

from tornado import ioloop, httpclient

import timeout_decorator


FOO = "foo"
BAR = "bar"


def lookup(d, *keys):
    d1 = copy.deepcopy(d)
    for k in keys:
        d1 = d1[k]
    return d1


def anomaly(x):
    return x > 100000 or x < 1  # 100000 = 0.1 sec


class Concurrent:
    class Endpoint:
        def __init__(self, url, app_name, span_names, transaction_name,
                     events_no=1000):
            self.url = url
            self.app_name = app_name
            self.span_names = span_names
            self.transaction_name = transaction_name
            self.events_no = events_no
            self.no_per_event = {
                "span": len(span_names),
                "transaction": 1
            }
            self.set_agent

        def count(self, name):
            return self.no_per_event.get(name, 0) * self.events_no

        def set_agent(self):
            if self.app_name in ("flask_app", "django_app"):
                self.agent = "elasticapm-python"
            elif self.app_name in ("express_app"):
                self.agent = "nodejs"
            else:
                raise Exception(
                    "Missing agent for app {}".format(self.app_name))

    def __init__(self, elasticsearch, endpoints, iters=1, index="apm-*"):
        self.num_reqs = 0
        self.index = index
        # TODO: improve ES handling
        self.elasticsearch = elasticsearch
        self.es = elasticsearch.es
        self.endpoints = endpoints
        self.iters = iters
        self.set_logger()

    def count(self, name):
        return sum(ep.count(name) for ep in self.endpoints)

    def set_logger(self):
        logger = logging.getLogger("logger")
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '[%(asctime)s] [%(process)s] [%(levelname)s] [%(funcName)s - \
            %(lineno)d]  %(message)s')
        handler.setFormatter(formatter)
        logger.propagate = False
        logger.addHandler(handler)
        self.logger = logger

    def handle(self, r):
        if r.code != 200:
            ioloop.IOLoop.instance().stop()
            message = "Bad response, aborting: {} - {} ({})".format(r.code, r.error, r.request_time)
            self.logger.error(message)
            raise Exception(message)

        self.num_reqs -= 1
        if self.num_reqs == 0:
            self.logger.info("Stopping tornado I/O loop")
            ioloop.IOLoop.instance().stop()

    def load_test(self):
        http_client = httpclient.AsyncHTTPClient(max_clients=4)
        for endpoint in self.endpoints:
            for _ in range(endpoint.events_no):
                self.num_reqs += 1
                http_client.fetch(endpoint.url, self.handle, method='GET',
                                  connect_timeout=90, request_timeout=120)

        self.logger.info("Starting tornado I/O loop")
        ioloop.IOLoop.instance().start()

    def check_counts(self, it, max_wait=60, backoff=.5):
        err = "queried for {}, expected {}, got {}"

        @timeout_decorator.timeout(max_wait)
        def assert_count(field, value, cnt):
            """wait a bit for doc count to reach expectation"""
            rs = {'count': -1}
            while rs['count'] < cnt:
                rs = self.es.count(index=self.index,
                                   body=self.elasticsearch.regexp_q(field, value))
                time.sleep(backoff)
            assert rs['count'] == cnt, err.format(value, cnt, rs)

        self.es.indices.refresh()

        transactions_count = self.count("transaction") * it
        assert_count("processor.event", "transaction", transactions_count)

        spans_count = self.count("span") * it
        assert_count("processor.event", "span", spans_count)

        transactions_sum = spans_sum = 0
        for ep in self.endpoints:
            for span_name in ep.span_names:
                count = ep.count("span") * it / len(ep.span_names)
                spans_sum += count
                assert_count("span.name", span_name, count)

            count = ep.count("transaction") * it
            transactions_sum += count
            transaction_q = {'query': {'bool': {'must': [
                {'term': {
                    'context.service.name': ep.app_name
                }},
                {'term': {
                    'transaction.name.keyword': ep.transaction_name
                }}
            ]}}}
            transaction_count = self.es.count(index=self.index, body=transaction_q)['count']
            assert transaction_count == count, err.format(
                "transactions per endpoint", count, transaction_count)

        assert transactions_count == transactions_sum, err.format(
            "transactions all endpoints", transactions_count, transactions_sum)
        assert spans_count == spans_sum, err.format(
            "spans all endpoints", spans_count, spans_sum)

    def check_content(self, it):
        for ep in self.endpoints:
            q = self.elasticsearch.regexp_q(
                "transaction.name", ep.transaction_name)
            rs = self.es.search(index=self.index, body=q)
            for hit in lookup(rs, 'hits', 'hits'):

                assert hit['_source']['processor'] == {'name': 'transaction',
                                                       'event': 'transaction'}

                transaction = lookup(hit, '_source', 'transaction')

                duration = lookup(transaction, 'duration', 'us')
                assert not anomaly(duration), duration

                timestamp = datetime.strptime(lookup(hit, '_source', '@timestamp'),
                                              '%Y-%m-%dT%H:%M:%S.%fZ')
                assert datetime.utcnow() - timedelta(minutes=it) < timestamp < datetime.utcnow(), \
                    "{} is too far of {} ".format(timestamp, datetime.utcnow())

                assert transaction['result'] == '200', transaction['result']
                assert transaction['type'] == 'request'

                context = lookup(hit, '_source', 'context')
                assert context['request']['method'] == "GET", context['request']['method']
                assert context['request']['url']['hostname'] == 'localhost'
                pathname = os.path.basename(os.path.normpath(ep.url))
                assert context['request']['url']['pathname'] == pathname, \
                    context['request']['url']['pathname']

                assert context['tags'] == {}, context

                app_name = lookup(context, 'service', 'name')
                assert app_name == ep.app_name, app_name

                agent = lookup(context, 'service', 'agent', 'name')
                assert agent == ep.agent, agent

                search = context['request']['url']['search']
                framework = lookup(context, 'service', 'framework', 'name')
                if agent == 'nodejs':
                    assert context['response']['status_code'] == 200, context['response']['status_code']
                    assert context['user'] == {}, context
                    assert context['custom'] == {}, context
                    assert search == '?', context
                    lang = lookup(context, 'service', 'runtime', 'name')
                    assert lang == "node", context
                    assert framework in ("express"), context
                elif agent == 'elasticapm-python':
                    assert search == '', context
                    lang = lookup(context, 'service', 'language', 'name')
                    assert lang == "python", context
                    assert framework in ("django", "flask"), context
                else:
                    raise Exception("Undefined agent {}".format(agent))

                spans_query = {'query': {'bool': {'must': [
                    {'term': {
                        'processor.event': 'span'
                    }},
                    {'term': {
                        'transaction.id': transaction['id']
                    }}
                ]}}}
                span_hits = lookup(self.es.search(self.index, body=spans_query), 'hits', 'hits')
                assert len(span_hits) == ep.no_per_event["span"]
                for span_hit in span_hits:
                    assert span_hit['_source']['processor'] == {'name': 'span', 'event': 'transaction'}

                    span = lookup(span_hit, '_source', 'span')
                    assert span["name"] == ep.span_name

                    span_context = lookup(span_hit, '_source', 'context')
                    span_app_name = lookup(span_context, 'service', 'name')
                    assert span_app_name == ep.app_name

                    span_start = lookup(span, 'start', 'us')
                    assert not anomaly(span_start), span_start

                    span_duration = lookup(span, 'duration', 'us')
                    assert not anomaly(span_duration), span_duration

                    assert span_duration < duration * 10, \
                        "span duration {} is more than 10X bigger than transaction duration{}".format(
                            span_duration, duration)

                    stacktrace = span['stacktrace']
                    assert 15 < len(stacktrace) < 30, \
                        "number of frames not expected, got {}, but this assertion might be too strict".format(
                            len(stacktrace))

                    fns = [frame['function'] for frame in stacktrace]
                    assert all(fns), fns
                    for attr in ['abs_path', 'line', 'filename']:
                        assert all(
                            frame.get(attr) for frame in stacktrace), stacktrace[0].keys()

    def run(self):
        self.logger.info("Testing started..")
        self.elasticsearch.clean()

        for it in range(1, self.iters + 1):
            self.logger.info("Sending batch {} / {}".format(it, self.iters))
            self.load_test()
            self.check_counts(it)
            self.check_content(it)
            self.logger.info("So far so good...")
        self.logger.info("ALL DONE")