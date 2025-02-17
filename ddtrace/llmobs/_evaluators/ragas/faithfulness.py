import json
import math
import traceback
from typing import List
from typing import Optional
from typing import Union

from ddtrace.internal.logger import get_logger
from ddtrace.internal.telemetry import telemetry_writer
from ddtrace.internal.telemetry.constants import TELEMETRY_LOG_LEVEL
from ddtrace.internal.utils.version import parse_version
from ddtrace.llmobs._constants import RAGAS_ML_APP_PREFIX


logger = get_logger(__name__)


class MiniRagas:
    """
    A helper class to store instances of ragas classes and functions
    that may or may not exist in a user's environment.
    """

    llm_factory = None
    RagasoutputParser = None
    faithfulness = None
    ensembler = None
    get_segmenter = None
    StatementFaithfulnessAnswers = None
    StatementsAnswers = None


def _get_ml_app_for_ragas_trace(span_event: dict) -> str:
    """
    The `ml_app` spans generated from traces of ragas will be named as `dd-ragas-<ml_app>`
    or `dd-ragas` if `ml_app` is not present in the span event.
    """
    tags = span_event.get("tags", [])  # list[str]
    ml_app = None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("ml_app:"):
            ml_app = tag.split(":")[1]
            break
    if not ml_app:
        return RAGAS_ML_APP_PREFIX
    return "{}-{}".format(RAGAS_ML_APP_PREFIX, ml_app)


def _get_faithfulness_instance() -> Optional[object]:
    """
    This helper function ensures the faithfulness instance used in
    ragas evaluator is updated with the latest ragas faithfulness
    instance AND has an non-null llm
    """
    if MiniRagas.faithfulness is None:
        return None
    ragas_faithfulness_instance = MiniRagas.faithfulness
    if not ragas_faithfulness_instance.llm:
        ragas_faithfulness_instance.llm = MiniRagas.llm_factory()
    return ragas_faithfulness_instance


class RagasFaithfulnessEvaluator:
    """A class used by EvaluatorRunner to conduct ragas faithfulness evaluations
    on LLM Observability span events. The job of an Evaluator is to take a span and
    submit evaluation metrics based on the span's attributes.
    """

    LABEL = "ragas_faithfulness"
    METRIC_TYPE = "score"

    def __init__(self, llmobs_service):
        """
        Initialize an evaluator that uses the ragas library to generate a faithfulness score on finished LLM spans.

        Faithfulness measures the factual consistency of an LLM's output against a given context.
        There are two LLM calls required to generate a faithfulness score - one to generate a set of statements from
        the answer, and another to measure the faithfulness of those statements against the context using natural
        language entailment.

        For more information, see https://docs.ragas.io/en/latest/concepts/metrics/faithfulness/

        The `ragas.metrics.faithfulness` instance is used for faithfulness scores. If there is no llm attribute set
        on this instance, it will be set to the default `llm_factory()` which uses openai.

        :param llmobs_service: An instance of the LLM Observability service used for tracing the evaluation and
                                      submitting evaluation metrics.

        Raises: NotImplementedError if the ragas library is not found or if ragas version is not supported.
        """
        self.llmobs_service = llmobs_service
        self.ragas_version = "unknown"
        telemetry_state = "ok"
        try:
            import ragas

            self.ragas_version = parse_version(ragas.__version__)
            if self.ragas_version >= (0, 2, 0) or self.ragas_version < (0, 1, 10):
                raise NotImplementedError(
                    "Ragas version: {} is not supported for `ragas_faithfulness` evaluator".format(self.ragas_version),
                )

            from ragas.llms import llm_factory

            MiniRagas.llm_factory = llm_factory

            from ragas.llms.output_parser import RagasoutputParser

            MiniRagas.RagasoutputParser = RagasoutputParser

            from ragas.metrics import faithfulness

            MiniRagas.faithfulness = faithfulness

            from ragas.metrics.base import ensembler

            MiniRagas.ensembler = ensembler

            from ragas.metrics.base import get_segmenter

            MiniRagas.get_segmenter = get_segmenter

            from ddtrace.llmobs._evaluators.ragas.models import StatementFaithfulnessAnswers

            MiniRagas.StatementFaithfulnessAnswers = StatementFaithfulnessAnswers

            from ddtrace.llmobs._evaluators.ragas.models import StatementsAnswers

            MiniRagas.StatementsAnswers = StatementsAnswers
        except Exception as e:
            telemetry_state = "fail"
            telemetry_writer.add_log(
                level=TELEMETRY_LOG_LEVEL.ERROR,
                message="Failed to import Ragas dependencies",
                stack_trace=traceback.format_exc(),
                tags={"ragas_version": self.ragas_version},
            )
            raise NotImplementedError("Failed to load dependencies for `ragas_faithfulness` evaluator") from e
        finally:
            telemetry_writer.add_count_metric(
                namespace="llmobs",
                name="evaluators.init",
                value=1,
                tags=(
                    ("evaluator_label", self.LABEL),
                    ("state", telemetry_state),
                    ("ragas_version", self.ragas_version),
                ),
            )

        self.ragas_faithfulness_instance = _get_faithfulness_instance()
        self.llm_output_parser_for_generated_statements = MiniRagas.RagasoutputParser(
            pydantic_object=MiniRagas.StatementsAnswers
        )
        self.llm_output_parser_for_faithfulness_score = MiniRagas.RagasoutputParser(
            pydantic_object=MiniRagas.StatementFaithfulnessAnswers
        )
        self.split_answer_into_sentences = MiniRagas.get_segmenter(
            language=self.ragas_faithfulness_instance.nli_statements_message.language, clean=False
        )

    def run_and_submit_evaluation(self, span_event: dict):
        if not span_event:
            return
        score_result_or_failure = self.evaluate(span_event)
        telemetry_writer.add_count_metric(
            "llmobs",
            "evaluators.run",
            1,
            tags=(
                ("evaluator_label", self.LABEL),
                ("state", score_result_or_failure if isinstance(score_result_or_failure, str) else "success"),
            ),
        )
        if isinstance(score_result_or_failure, float):
            self.llmobs_service.submit_evaluation(
                span_context={"trace_id": span_event.get("trace_id"), "span_id": span_event.get("span_id")},
                label=RagasFaithfulnessEvaluator.LABEL,
                metric_type=RagasFaithfulnessEvaluator.METRIC_TYPE,
                value=score_result_or_failure,
            )

    def evaluate(self, span_event: dict) -> Union[float, str]:
        """
        Performs a faithfulness evaluation on a span event, returning either
            - faithfulness score (float) OR
            - failure reason (str)
        If the ragas faithfulness instance does not have `llm` set, we set `llm` using the `llm_factory()`
        method from ragas which defaults to openai's gpt-4o-turbo.
        """
        self.ragas_faithfulness_instance = _get_faithfulness_instance()
        if not self.ragas_faithfulness_instance:
            return "fail_faithfulness_is_none"

        score, question, answer, context, statements, faithfulness_list = math.nan, None, None, None, None, None

        with self.llmobs_service.workflow(
            "dd-ragas.faithfulness", ml_app=_get_ml_app_for_ragas_trace(span_event)
        ) as ragas_faithfulness_workflow:
            try:
                faithfulness_inputs = self._extract_faithfulness_inputs(span_event)
                if faithfulness_inputs is None:
                    logger.debug(
                        "Failed to extract question and context from span sampled for ragas_faithfulness evaluation"
                    )
                    return "fail_extract_faithfulness_inputs"

                question = faithfulness_inputs["question"]
                answer = faithfulness_inputs["answer"]
                context = faithfulness_inputs["context"]

                statements = self._create_statements(question, answer)
                if statements is None:
                    logger.debug("Failed to create statements from answer for `ragas_faithfulness` evaluator")
                    return "statements_is_none"

                faithfulness_list = self._create_verdicts(context, statements)
                if faithfulness_list is None:
                    logger.debug("Failed to create faithfulness list `ragas_faithfulness` evaluator")
                    return "statements_create_faithfulness_list"

                score = self._compute_score(faithfulness_list)
                if math.isnan(score):
                    logger.debug("Score computation returned NaN for `ragas_faithfulness` evaluator")
                    return "statements_compute_score"

                return score
            finally:
                self.llmobs_service.annotate(
                    span=ragas_faithfulness_workflow,
                    input_data=span_event,
                    output_data=score,
                    metadata={
                        "statements": statements,
                        "faithfulness_list": faithfulness_list.dicts() if faithfulness_list is not None else None,
                    },
                )

    def _create_statements(self, question: str, answer: str) -> Optional[List[str]]:
        with self.llmobs_service.workflow("dd-ragas.create_statements"):
            self.llmobs_service.annotate(
                input_data={"question": question, "answer": answer},
            )
            statements_prompt = self._create_statements_prompt(answer=answer, question=question)

            """LLM step to break down the answer into simpler statements"""
            statements = self.ragas_faithfulness_instance.llm.generate_text(statements_prompt)

            statements = self.llm_output_parser_for_generated_statements.parse(statements.generations[0][0].text)

            if statements is None:
                return None
            statements = [item["simpler_statements"] for item in statements.dicts()]
            statements = [item for sublist in statements for item in sublist]

            self.llmobs_service.annotate(
                output_data=statements,
            )
            if not isinstance(statements, List):
                return None
            return statements

    def _create_verdicts(self, context: str, statements: List[str]):
        """
        Returns: `StatementFaithfulnessAnswers` model detailing which statements are faithful to the context
        """
        with self.llmobs_service.workflow("dd-ragas.create_verdicts") as create_verdicts_workflow:
            self.llmobs_service.annotate(
                span=create_verdicts_workflow,
                input_data=statements,
            )
            """Check which statements contradict the conntext"""
            raw_nli_results = self.ragas_faithfulness_instance.llm.generate_text(
                self._create_natural_language_inference_prompt(context, statements)
            )
            if len(raw_nli_results.generations) == 0:
                return None

            reproducibility = getattr(self.ragas_faithfulness_instance, "_reproducibility", 1)

            raw_nli_results_texts = [raw_nli_results.generations[0][i].text for i in range(reproducibility)]

            raw_faithfulness_list = [
                faith.dicts()
                for faith in [
                    self.llm_output_parser_for_faithfulness_score.parse(text) for text in raw_nli_results_texts
                ]
                if faith is not None
            ]

            if len(raw_faithfulness_list) == 0:
                return None

            # collapse multiple generations into a single faithfulness list
            faithfulness_list = MiniRagas.ensembler.from_discrete(raw_faithfulness_list, "verdict")  # type: ignore
            try:
                return MiniRagas.StatementFaithfulnessAnswers.parse_obj(faithfulness_list)  # type: ignore
            except Exception as e:
                logger.debug("Failed to parse faithfulness_list", exc_info=e)
                return None
            finally:
                self.llmobs_service.annotate(
                    span=create_verdicts_workflow,
                    output_data=faithfulness_list,
                )

    def _extract_faithfulness_inputs(self, span_event: dict) -> Optional[dict]:
        """
        Extracts the question, answer, and context used as inputs to faithfulness
        evaluation from a span event.

        question - input.prompt.variables.question OR input.messages[-1].content
        context - input.prompt.variables.context
        answer - output.messages[-1].content
        """
        with self.llmobs_service.workflow("dd-ragas.extract_faithfulness_inputs") as extract_inputs_workflow:
            self.llmobs_service.annotate(span=extract_inputs_workflow, input_data=span_event)
            question, answer, context = None, None, None

            meta_io = span_event.get("meta")
            if meta_io is None:
                return None

            meta_input = meta_io.get("input")
            meta_output = meta_io.get("output")

            if not (meta_input and meta_output):
                return None

            prompt = meta_input.get("prompt")
            if prompt is None:
                logger.debug("Failed to extract `prompt` from span for `ragas_faithfulness` evaluation")
                return None
            prompt_variables = prompt.get("variables")

            input_messages = meta_input.get("messages")

            messages = meta_output.get("messages")
            if messages is not None and len(messages) > 0:
                answer = messages[-1].get("content")

            if prompt_variables:
                question = prompt_variables.get("question")
                context = prompt_variables.get("context")

            if not question and len(input_messages) > 0:
                question = input_messages[-1].get("content")

            self.llmobs_service.annotate(
                span=extract_inputs_workflow, output_data={"question": question, "context": context, "answer": answer}
            )
            if any(field is None for field in (question, context, answer)):
                logger.debug("Failed to extract inputs required for faithfulness evaluation")
                return None

            return {"question": question, "context": context, "answer": answer}

    def _create_statements_prompt(self, answer, question):
        # Returns: `ragas.llms.PromptValue` object
        with self.llmobs_service.task("dd-ragas.create_statements_prompt"):
            sentences = self.split_answer_into_sentences.segment(answer)
            sentences = [sentence for sentence in sentences if sentence.strip().endswith(".")]
            sentences = "\n".join([f"{i}:{x}" for i, x in enumerate(sentences)])
            return self.ragas_faithfulness_instance.statement_prompt.format(
                question=question, answer=answer, sentences=sentences
            )

    def _create_natural_language_inference_prompt(self, context_str: str, statements: List[str]):
        # Returns: `ragas.llms.PromptValue` object
        with self.llmobs_service.task("dd-ragas.create_natural_language_inference_prompt"):
            prompt_value = self.ragas_faithfulness_instance.nli_statements_message.format(
                context=context_str, statements=json.dumps(statements)
            )
            return prompt_value

    def _compute_score(self, faithfulness_list) -> float:
        """
        Args:
            faithfulness_list (StatementFaithfulnessAnswers): a list of statements and their faithfulness verdicts
        """
        with self.llmobs_service.task("dd-ragas.compute_score"):
            faithful_statements = sum(1 if answer.verdict else 0 for answer in faithfulness_list.__root__)
            num_statements = len(faithfulness_list.__root__)
            if num_statements:
                score = faithful_statements / num_statements
            else:
                score = math.nan
            self.llmobs_service.annotate(
                metadata={
                    "faithful_statements": faithful_statements,
                    "num_statements": num_statements,
                },
                output_data=score,
            )
            return score
