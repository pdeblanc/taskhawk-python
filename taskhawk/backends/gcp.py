import json
import logging
import typing
from contextlib import contextmanager, ExitStack
from datetime import datetime
from typing import cast, Generator

from unittest import mock
from google.api_core.exceptions import DeadlineExceeded
from google.auth import environment_vars as google_env_vars, default as google_auth_default
from google.cloud import pubsub_v1
from google.cloud.pubsub_v1.futures import Future
from google.cloud.pubsub_v1.proto.pubsub_pb2 import ReceivedMessage

from taskhawk.backends.base import (
    TaskhawkPublisherBaseBackend,
    TaskhawkConsumerBaseBackend,
)
from taskhawk.backends.utils import override_env
from taskhawk.conf import settings
from taskhawk.models import Message, Priority


logger = logging.getLogger(__name__)


@contextmanager
def _seed_credentials() -> Generator[None, None, None]:
    """
    Seed environment with explicitly set credentials. Normally we'd stay away from mucking with environment vars,
    however the logic to decode `GOOGLE_APPLICATION_CREDENTIALS` isn't simple, so we let Google libraries handle it.
    """
    with ExitStack() as stack:
        if settings.GOOGLE_APPLICATION_CREDENTIALS:
            stack.enter_context(override_env(google_env_vars.CREDENTIALS, settings.GOOGLE_APPLICATION_CREDENTIALS))

        if settings.GOOGLE_CLOUD_PROJECT:
            stack.enter_context(override_env(google_env_vars.PROJECT, settings.GOOGLE_CLOUD_PROJECT))

        yield


def _auto_discover_project() -> None:
    """
    Auto discover Google project id from credentials. If project id is explicitly set, just use that.
    """
    if not settings.GOOGLE_CLOUD_PROJECT:
        # discover project from credentials
        # there's no way to get this from the Client objects, so we reload the credentials
        _, project = google_auth_default()
        assert project, "couldn't discover project"
        setattr(settings, 'GOOGLE_CLOUD_PROJECT', project)


def get_google_cloud_project() -> str:
    if not settings.GOOGLE_CLOUD_PROJECT:
        with _seed_credentials():
            _auto_discover_project()
    return settings.GOOGLE_CLOUD_PROJECT


# TODO move to dataclasses in py3.7
class GoogleMetadata:
    def __init__(self, ack_id: str, publish_time: datetime, delivery_attempt: int):
        self._ack_id = ack_id
        self._publish_time: datetime = publish_time
        self._delivery_attempt = delivery_attempt

    @property
    def ack_id(self) -> str:
        """
        The ID used to ack the message
        """
        return self._ack_id

    @property
    def publish_time(self) -> datetime:
        """
        Time this message was originally published to Pub/Sub
        """
        return self._publish_time

    @property
    def delivery_attempt(self) -> int:
        """
        The delivery attempt counter received from Pub/Sub.
        The first delivery of a given message will have this value as 1. The value
        is calculated at best effort and is approximate.
        """
        return self._delivery_attempt

    def __eq__(self, o: object) -> bool:
        if not isinstance(o, GoogleMetadata):
            return False

        return self.ack_id == o.ack_id and self.delivery_attempt == o.delivery_attempt

    def __ne__(self, o: object) -> bool:
        return not self.__eq__(o)

    def __str__(self) -> str:
        return repr(self)

    def __repr__(self) -> str:
        return (
            'GoogleMetadata('
            f'ack_id={self.ack_id}, publish_time={self.publish_time}, delivery_attempt={self.delivery_attempt}'
            ')'
        )

    def __hash__(self) -> int:
        return hash((self.ack_id, self.publish_time))


def get_priority_suffix(priority: Priority) -> str:
    if priority is Priority.high:
        return '-high-priority'
    elif priority is Priority.low:
        return '-low-priority'
    elif priority is Priority.bulk:
        return '-bulk'
    return ''


class GooglePubSubAsyncPublisherBackend(TaskhawkPublisherBaseBackend):
    def __init__(self, priority: Priority) -> None:
        self._publisher = None
        if not settings.TASKHAWK_SYNC:
            self._topic_path = pubsub_v1.PublisherClient.topic_path(
                get_google_cloud_project(),
                f'taskhawk-{settings.TASKHAWK_QUEUE.lower()}{get_priority_suffix(priority)}',
            )

    @property
    def publisher(self):
        if self._publisher is None:
            with _seed_credentials():
                self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    def publish_to_topic(
        self, topic_path: str, data: bytes, attrs: typing.Optional[typing.Mapping] = None
    ) -> typing.Union[str, Future]:
        """
        Publishes to a Google Pub/Sub topic and returns a future that represents the publish API call. These API calls
        are batched for better performance.
        """
        attrs = attrs or {}
        attrs = dict((str(key), str(value)) for key, value in attrs.items())
        return self.publisher.publish(topic_path, data=data, **attrs)

    def _mock_queue_message(self, message: Message) -> mock.Mock:
        gcp_message = mock.Mock()
        gcp_message.message = mock.Mock()
        gcp_message.message.data = json.dumps(message.as_dict()).encode('utf8')
        gcp_message.ack_id = 'test-receipt'
        return gcp_message

    def _publish(self, message: Message, payload: str, headers: typing.Optional[typing.Mapping] = None,) -> str:
        return self.publish_to_topic(self._topic_path, payload.encode("utf8"), headers)


class GooglePubSubPublisherBackend(GooglePubSubAsyncPublisherBackend):
    def publish_to_topic(
        self, topic_path: str, data: bytes, attrs: typing.Optional[typing.Mapping] = None
    ) -> typing.Union[str, Future]:
        return cast(Future, super().publish_to_topic(topic_path, data, attrs)).result()


class GooglePubSubConsumerBackend(TaskhawkConsumerBaseBackend):
    def __init__(self, priority: Priority, dlq=False) -> None:
        self._publisher = None
        self._subscriber = None
        if not settings.TASKHAWK_SYNC:
            cloud_project = get_google_cloud_project()
            self._subscription_path: str = pubsub_v1.SubscriberClient.subscription_path(
                cloud_project,
                f'taskhawk-{settings.TASKHAWK_QUEUE.lower()}{get_priority_suffix(priority)}{"-dlq" if dlq else ""}',
            )
            self._dlq_topic_path: str = pubsub_v1.PublisherClient.topic_path(
                cloud_project, f'taskhawk-{settings.TASKHAWK_QUEUE.lower()}{get_priority_suffix(priority)}-dlq',
            )

    @property
    def publisher(self):
        if self._publisher is None:
            with _seed_credentials():
                self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    @property
    def subscriber(self):
        if self._subscriber is None:
            with _seed_credentials():
                self._subscriber = pubsub_v1.SubscriberClient()
        return self._subscriber

    def pull_messages(self, num_messages: int = 1, visibility_timeout: int = None) -> typing.List:
        try:
            received_messages = self.subscriber.pull(
                self._subscription_path, num_messages, retry=None, timeout=settings.GOOGLE_PUBSUB_READ_TIMEOUT_S,
            ).received_messages
            return received_messages
        except DeadlineExceeded:
            logger.debug(f"Pulling deadline exceeded subscription={self._subscription_path}")
            return []

    def process_message(self, queue_message: ReceivedMessage) -> None:
        self.message_handler(
            queue_message.message.data.decode(),
            GoogleMetadata(
                queue_message.ack_id, queue_message.message.publish_time, queue_message.message.delivery_attempt,
            ),
        )

    def delete_message(self, queue_message: ReceivedMessage) -> None:
        self.subscriber.acknowledge(self._subscription_path, [queue_message.ack_id])

    @staticmethod
    def pre_process_hook_kwargs(queue_message: ReceivedMessage) -> dict:
        return {'google_pubsub_message': queue_message}

    @staticmethod
    def post_process_hook_kwargs(queue_message: ReceivedMessage) -> dict:
        return {'google_pubsub_message': queue_message}

    def extend_visibility_timeout(self, visibility_timeout_s: int, metadata: GoogleMetadata) -> None:
        """
        Extends visibility timeout of a message on a given priority queue for long running tasks.
        """
        if visibility_timeout_s < 0 or visibility_timeout_s > 600:
            raise ValueError("Invalid visibility_timeout_s")
        self.subscriber.modify_ack_deadline(self._subscription_path, [metadata.ack_id], visibility_timeout_s)

    def requeue_dead_letter(self, num_messages: int = 10, visibility_timeout: int = None) -> None:
        """
        Re-queues everything in the Taskhawk DLQ back into the Taskhawk queue.

        :param num_messages: Maximum number of messages to fetch in one call. Defaults to 10.
        :param visibility_timeout: The number of seconds the message should remain invisible to other queue readers.
        Defaults to None, which is queue default
        """
        topic_path = self._dlq_topic_path[: len('-dlq')]
        logging.info("Re-queueing messages from {} to {}".format(self._subscription_path, topic_path))
        while True:
            queue_messages = self.pull_messages(num_messages=num_messages, visibility_timeout=visibility_timeout)
            if not queue_messages:
                break

            logging.info("got {} messages from dlq".format(len(queue_messages)))
            for queue_message in queue_messages:
                try:
                    if visibility_timeout:
                        self.subscriber.modify_ack_deadline(
                            self._subscription_path, [queue_message.ack_id], visibility_timeout
                        )

                    future = self.publisher.publish(
                        topic_path, data=queue_message.message.data, **queue_message.message.attributes
                    )
                    # wait for success
                    future.result()
                    logger.debug(
                        'Re-queued message from DLQ {} to {}'.format(self._subscription_path, topic_path),
                        extra={'message_id': queue_message.message.message_id},
                    )

                    self.delete_message(queue_message)
                except Exception:
                    logger.exception(
                        'Exception in requeue message from {} to {}'.format(self._subscription_path, topic_path)
                    )

            logging.info("Re-queued {} messages".format(len(queue_messages)))
