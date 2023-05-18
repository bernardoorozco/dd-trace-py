from ddtrace.internal.datastreams.processor import DataStreamsProcessor
import time


def test_data_streams_processor():
    processor = DataStreamsProcessor("http://localhost:8126")
    now = time.time()
    processor.on_checkpoint_creation(1, 2, ["direction:out", "topic:topicA", "type:kafka"], now, 1, 1)
    processor.on_checkpoint_creation(1, 2, ["direction:out", "topic:topicA", "type:kafka"], now, 1, 2)
    processor.on_checkpoint_creation(1, 2, ["direction:out", "topic:topicA", "type:kafka"], now, 1, 4)
    processor.on_checkpoint_creation(2, 4, ["direction:in", "topic:topicA", "type:kafka"], now, 1, 2)
    now_ns = int(now * 1e9)
    bucket_time_ns = int(now_ns - (now_ns % 1e10))
    aggr_key_1 = (",".join(["direction:out", "topic:topicA", "type:kafka"]), 1, 2)
    aggr_key_2 = (",".join(["direction:in", "topic:topicA", "type:kafka"]), 2, 4)
    assert processor._buckets[bucket_time_ns][aggr_key_1].full_pathway_latency.count == 3
    assert processor._buckets[bucket_time_ns][aggr_key_2].full_pathway_latency.count == 1
    assert (
        abs(processor._buckets[bucket_time_ns][aggr_key_1].full_pathway_latency.get_quantile_value(1) - 4) <= 4 * 0.008
    )  # relative accuracy of 0.00775
    assert (
        abs(processor._buckets[bucket_time_ns][aggr_key_2].full_pathway_latency.get_quantile_value(1) - 2) <= 2 * 0.008
    )  # relative accuracy of 0.00775
