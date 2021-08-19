import abc
import asyncio
from collections import defaultdict, namedtuple
from functools import partial
from time import monotonic
from typing import Callable, Coroutine, Dict, Iterator, List, Set
from uuid import UUID, uuid4

from . import metrics
from .logging import logger
from .util import ensure_async, split_collection

QueuedMessage = namedtuple("QueuedMessage", ["topic", "contents"])

__all_sessions__ = object()


# TODO: no more checking for messages we've seen before -- that can merged into
# a data validation hook, make an example!
class AbstractPubSubManager(abc.ABC):
    def __init__(self):
        self.outbound_queue: asyncio.Queue[QueuedMessage] = None
        self.outbound_workers: List[asyncio.Task] = []

        self.inbound_queue: asyncio.Queue[QueuedMessage] = None
        self.inbound_workers: List[asyncio.Task] = []

        self.callback_delegation_workers = 1

        self.poll_workers: List[asyncio.Task] = []
        self.subscribed_topics_by_session: Dict[str, Set] = defaultdict(set)

        self.callbacks_by_id: Dict[UUID, Coroutine] = {}

    async def initialize(
        self,
        *,
        queue_size=0,
        inbound_workers=1,
        outbound_workers=1,
        poll_workers=1,
        callback_delegation_workers=None,
    ):
        self.outbound_queue = asyncio.Queue(queue_size)
        self.inbound_queue = asyncio.Queue(queue_size)
        self.callback_delegation_workers = (
            callback_delegation_workers or self.callback_delegation_workers
        )

        for i in range(outbound_workers):
            self.outbound_workers.append(asyncio.create_task(self._outbound_worker()))

        for i in range(inbound_workers):
            self.inbound_workers.append(asyncio.create_task(self._inbound_worker()))

        for i in range(poll_workers):
            self.poll_workers.append(asyncio.create_task(self._poll()))

    async def shutdown(self, now=False):
        if not now:
            await self._drain_queues()

        self.inbound_queue = None
        metrics.INBOUND_QUEUE_SIZE.set(0)

        self.outbound_queue = None
        metrics.OUTBOUND_QUEUE_SIZE.set(0)

        for worker in self.outbound_workers:
            worker.cancel()

        for worker in self.inbound_workers:
            worker.cancel()

        for worker in self.poll_workers:
            worker.cancel()

        await asyncio.gather(
            *self.outbound_workers,
            *self.inbound_workers,
            *self.poll_workers,
            return_exceptions=True,
        )

        self.outbound_workers.clear()
        self.inbound_workers.clear()
        self.poll_workers.clear()

        self.subscribed_topics_by_session.clear()
        metrics.SUBSCRIBED_TOPICS.set(0)

        self.callbacks_by_id.clear()
        metrics.REGISTERED_CALLBACKS.set(0)

    async def _drain_queues(self):
        await self.inbound_queue.join()
        await self.outbound_queue.join()

    def send(self, topic_name: str, message):
        self.outbound_queue.put_nowait(QueuedMessage(topic_name, message))
        metrics.OUTBOUND_QUEUE_SIZE.inc()

    async def subscribe_to_topic(self, topic_name: str, session_id=__all_sessions__):
        if not self.is_subscribed_to_topic(topic_name):
            logger.info(f"Creating subscription to topic '{topic_name}'")
            await self._create_topic_subscription(topic_name)
            metrics.SUBSCRIBED_TOPICS.inc()

        logger.debug(f"Adding topic '{topic_name}' to session cache: {session_id}")
        self.subscribed_topics_by_session[session_id].add(topic_name)

    @abc.abstractmethod
    async def _create_topic_subscription(self, topic_name: str):
        pass

    async def unsubscribe_from_topic(self, topic_name: str, session_id=__all_sessions__):
        if self.is_subscribed_to_topic(topic_name, session_id):
            logger.debug(f"Removing topic '{topic_name}' from session cache: {session_id}")
            self.subscribed_topics_by_session[session_id].remove(topic_name)

        if not self.is_subscribed_to_topic(topic_name):
            logger.info(f"No more subscriptions to topic {topic_name}, cleaning up...")
            await self._cleanup_topic_subscription(topic_name)
            metrics.SUBSCRIBED_TOPICS.dec()

    @property
    def subscribed_topics(self) -> Set[str]:
        return set(
            [item for sublist in self.subscribed_topics_by_session.values() for item in sublist]
        )

    def is_subscribed_to_topic(self, topic_name: str, session_id=None) -> bool:
        if session_id is not None:
            return topic_name in self.subscribed_topics_by_session[session_id]
        else:
            return topic_name in self.subscribed_topics

    @abc.abstractmethod
    async def _cleanup_topic_subscription(self, topic_name: str):
        pass

    def callback(self, fn: Callable) -> Callable:
        fn = ensure_async(fn)
        cb_id = str(uuid4())
        logger.debug(f"Registering callback: '{cb_id}'")
        self.callbacks_by_id[cb_id] = fn
        metrics.REGISTERED_CALLBACKS.inc()
        return partial(self._detach_callback, cb_id)

    def _detach_callback(self, cb_id: str):
        callback = self.callbacks_by_id.get(cb_id)
        if callback is not None:
            logger.info(f"Detaching callback: '{cb_id}'")
            del self.callbacks_by_id[cb_id]
            metrics.REGISTERED_CALLBACKS.dec()

    async def _outbound_worker(self):
        while True:
            message = await self.outbound_queue.get()
            try:
                await self._publish(message)
                metrics.OUTBOUND_MESSAGES_SENT.inc()
            except Exception:
                logger.exception("Uncaught exception found while publishing message")
                metrics.PUBLISH_MESSAGE_EXCEPTIONS.inc()
            self.outbound_queue.task_done()
            metrics.OUTBOUND_QUEUE_SIZE.dec()

    @abc.abstractmethod
    async def _publish(self, message: QueuedMessage):
        """The action needed to publish the message to the backend pubsub
        implementation.

        This will only be called by the outbound worker.
        """
        pass

    async def _inbound_worker(self):
        while True:
            message = await self.inbound_queue.get()
            contents = message.contents
            callback_ids = list(self.callbacks_by_id.keys())
            await asyncio.gather(
                *[
                    self._delegate_to_callbacks(contents, slice)
                    for slice in split_collection(callback_ids, self.callback_delegation_workers)
                ]
            )
            self.inbound_queue.task_done()
            metrics.INBOUND_QUEUE_SIZE.dec()

    async def _delegate_to_callbacks(self, contents, callback_ids: Iterator[UUID]):
        for id in callback_ids:
            cb = self.callbacks_by_id.get(id)
            if cb is not None:
                try:
                    enter = monotonic()
                    await cb(contents)
                    diff = monotonic() - enter
                    metrics.CALLBACK_DURATION.observe(diff)
                except Exception:
                    logger.exception("Uncaught exception encountered while delegating to callback")
                    metrics.CALLBACK_EXCEPTIONS.inc()

    @abc.abstractmethod
    async def _poll(self):
        pass

    def schedule_for_delivery(self, message: QueuedMessage):
        self.inbound_queue.put_nowait(message)
        metrics.INBOUND_QUEUE_SIZE.inc()
        metrics.INBOUND_MESSAGES_RECEIVED.inc()

    def get_session(self):
        return PubSubSession(self)


class PubSubSession:
    def __init__(self, parent: AbstractPubSubManager) -> None:
        self.id: str = str(uuid4())
        self.parent: AbstractPubSubManager = parent
        self._unregister_callbacks_by_id: Dict[str, Callable] = {}

    @property
    def subscribed_topics(self) -> Set[str]:
        return self.parent.subscribed_topics_by_session[self.id]

    def is_subscribed_to_topic(self, topic_name: str) -> bool:
        return topic_name in self.subscribed_topics

    async def subscribe_to_topic(self, topic_name: str):
        return await self.parent.subscribe_to_topic(topic_name, self.id)

    async def unsubscribe_from_topic(self, topic_name: str):
        return await self.parent.unsubscribe_from_topic(topic_name, self.id)

    def callback(self, fn: Callable):
        unregister_callback_id = str(uuid4())
        unregister_callback = self.parent.callback(fn)
        self._unregister_callbacks_by_id[unregister_callback_id] = unregister_callback
        return partial(self._detach_callback, unregister_callback_id)

    def _detach_callback(self, cb_id: str):
        # We do a second layer of ID-Callback caching here so that we can support
        # the detaching of callbacks mid-session but also so that we can pull the
        # session's cache of unregister callback methods and run them in batch
        # during cleanup.
        parent_detach_callback = self._unregister_callbacks_by_id.get(cb_id)
        if parent_detach_callback is not None:
            del self._unregister_callbacks_by_id[cb_id]
            return parent_detach_callback()

    async def stop(self):
        for cb in self._unregister_callbacks_by_id.values():
            cb()

        self._unregister_callbacks_by_id.clear()

        await asyncio.gather(
            *[self.unsubscribe_from_topic(topic_name) for topic_name in self.subscribed_topics]
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
