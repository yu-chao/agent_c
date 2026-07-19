import pytest

from agent_runtime.models import (
    ModelRequest,
    TextBlock,
    ToolCall,
    ToolResult,
    create_model_provider,
)
from agent_runtime.models.openai import OpenAIProvider
from agent_runtime.models.anthropic import AnthropicProvider
from agent_runtime.models.errors import PermanentModelError, RetryableModelError
from agent_runtime.tools.registry import ToolSpec


def test_openai_tool_schema_converts_internal_tool_spec():
    provider = OpenAIProvider(client=object(), model="gpt-5")
    tool = ToolSpec(
        name="read_file",
        description="Read a file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )

    converted = provider.convert_tools([tool])

    assert converted == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a file.",
            "parameters": tool.input_schema,
            "strict": True,
        }
    ]


def test_openai_provider_converts_function_call_to_tool_call():
    response = FakeOpenAIResponse(
        id="resp_123",
        output=[
            FakeOutputText("Inspecting."),
            FakeFunctionCall(
                call_id="call_1",
                name="read_file",
                arguments='{"path": "README.md"}',
            ),
        ],
    )
    provider = OpenAIProvider(client=FakeOpenAIClient(response), model="gpt-5")

    result = provider.generate(ModelRequest(messages=[], system="sys", tools=[]))

    assert result.response_id == "resp_123"
    assert result.blocks == [
        TextBlock(text="Inspecting."),
        ToolCall(id="call_1", name="read_file", input={"path": "README.md"}),
    ]


def test_openai_provider_sends_tool_results_as_function_call_output():
    client = FakeOpenAIClient(FakeOpenAIResponse(id="resp_2", output=[]))
    provider = OpenAIProvider(client=client, model="gpt-5")

    provider.generate(
        ModelRequest(
            messages=[ToolResult(tool_call_id="call_1", content="ok")],
            system="sys",
            tools=[],
            previous_response_id="resp_1",
        )
    )

    assert client.last_kwargs["previous_response_id"] == "resp_1"
    assert client.last_kwargs["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "ok",
        }
    ]


def test_anthropic_provider_converts_tool_call_history_to_tool_use():
    provider = AnthropicProvider(client=object(), model="claude-sonnet-4")

    converted = provider._convert_messages(
        [ToolCall(id="call_1", name="read_file", input={"path": "README.md"})]
    )

    assert converted == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                }
            ],
        }
    ]


def test_factory_creates_openai_provider_from_config(monkeypatch):
    monkeypatch.setenv("provider", "openai")
    monkeypatch.setenv("MODEL_ID", "gpt-5")
    provider = create_model_provider(clients={"openai": object()})

    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "gpt-5"


@pytest.mark.parametrize('status_code', [408, 409, 429, 500, 503])
def test_openai_provider_classifies_retryable_http_failures(status_code):
    provider = OpenAIProvider(
        client=FailingOpenAIClient(HttpError(status_code)), model='gpt-5'
    )

    with pytest.raises(RetryableModelError):
        provider.generate(ModelRequest(messages=[], system='sys'))


@pytest.mark.parametrize('status_code', [400, 401, 403, 404, 422])
def test_anthropic_provider_classifies_permanent_http_failures(status_code):
    provider = AnthropicProvider(
        client=FailingAnthropicClient(HttpError(status_code)), model='claude'
    )

    with pytest.raises(PermanentModelError):
        provider.generate(ModelRequest(messages=[], system='sys'))


class FakeOpenAIClient:
    def __init__(self, response):
        self.responses = self
        self.response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response


class FailingOpenAIClient:
    def __init__(self, error):
        self.responses = self
        self.error = error

    def create(self, **kwargs):
        raise self.error


class FailingAnthropicClient:
    def __init__(self, error):
        self.messages = self
        self.error = error

    def create(self, **kwargs):
        raise self.error


class HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f'HTTP {status_code}')
        self.status_code = status_code


class FakeOpenAIResponse:
    def __init__(self, id, output):
        self.id = id
        self.output = output


class FakeOutputText:
    type = "message"

    def __init__(self, text):
        self.content = [type("Content", (), {"type": "output_text", "text": text})()]


class FakeFunctionCall:
    type = "function_call"

    def __init__(self, call_id, name, arguments):
        self.call_id = call_id
        self.name = name
        self.arguments = arguments
