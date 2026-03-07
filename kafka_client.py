import json
import time
from kafka import KafkaProducer, KafkaConsumer
from kafka.admin import KafkaAdminClient, NewTopic

class AuroraKafkaClient:
    def __init__(self, bootstrap_servers, api_version=(3, 4, 0)):
        self.bootstrap_servers = bootstrap_servers
        self.api_version = api_version

    def _wait_for_connection(self, func, *args, **kwargs):
        """Generic retry wrapper for Kafka operations."""
        while True:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                print(f"  [Kafka] Connection deferred: {e}. Retrying in 2s...")
                time.sleep(2)

class AuroraProducer(AuroraKafkaClient):
    def __init__(self, bootstrap_servers, api_version=(3, 4, 0)):
        super().__init__(bootstrap_servers, api_version)
        self.producer = self._wait_for_connection(self._create_producer)

    def _create_producer(self):
        return KafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            api_version=self.api_version,
            retries=5
        )

    def send_log(self, topic, log_entry, key=None):
        """Sends a log entry to a topic, using trace.id as default key."""
        kafka_key = key or log_entry.get('trace.id') or log_entry.get('id')
        if kafka_key:
            kafka_key = str(kafka_key).encode('utf-8')
        
        self.producer.send(topic, value=log_entry, key=kafka_key)

    def flush(self):
        self.producer.flush()

    def close(self):
        self.producer.close()

    def ensure_topic(self, topic_name, partitions=1, replication=1):
        """Ensures a topic exists, creating it if necessary."""
        try:
            admin = KafkaAdminClient(
                bootstrap_servers=self.bootstrap_servers,
                api_version=self.api_version
            )
            if topic_name not in admin.list_topics():
                print(f"  [Kafka] Creating topic: {topic_name}")
                topic = NewTopic(name=topic_name, num_partitions=partitions, replication_factor=replication)
                admin.create_topics([topic])
                time.sleep(1)
            admin.close()
        except Exception as e:
            print(f"  [Kafka] Topic check failed: {e}")

class AuroraConsumer(AuroraKafkaClient):
    def __init__(self, topics, group_id, bootstrap_servers, api_version=(3, 4, 0), auto_offset='earliest'):
        super().__init__(bootstrap_servers, api_version)
        self.topics = topics if isinstance(topics, list) else [topics]
        self.group_id = group_id
        self.auto_offset = auto_offset
        self.consumer = self._wait_for_connection(self._create_consumer)

    def _create_consumer(self):
        return KafkaConsumer(
            *self.topics,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_id,
            auto_offset_reset=self.auto_offset,
            value_deserializer=lambda m: json.loads(m.decode('utf-8')),
            api_version=self.api_version
        )

    def __iter__(self):
        return self.consumer

    def close(self):
        self.consumer.close()
