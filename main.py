"""
Multi-Agent Expense Approval System
====================================
OpenAI Agents SDK — orchestrator + sub-agents as tools pattern.

Each sub-agent owns its own integration:
  • ExpenseParserAgent    — downloads the file from UiPath Storage Bucket
  • PolicyValidatorAgent  — retrieves policy via UiPath Context Grounding
  • ExpenseProcessorAgent — assembles the final structured expense record

The OrchestratorAgent keeps full control at all times.
Sub-agents are exposed as tools via Agent.as_tool()

"""

import os
from agents import Agent, function_tool
from uipath.platform import UiPath
from PyPDF2 import PdfReader
from agents.models import _openai_shared
from uipath_openai_agents.chat import UiPathChatOpenAI
from uipath_openai_agents.chat.supported_models import OpenAIModels
from pydantic import BaseModel
from typing import Optional


# ─────────────────────────────────────────────────────────────
# 0.  Config  (override via environment variables in production)
# ─────────────────────────────────────────────────────────────

STORAGE_BUCKET_NAME     = os.getenv("STORAGE_BUCKET_NAME", "ExpenseReceipts")
STORAGE_BUCKET_FOLDER   = os.getenv("STORAGE_BUCKET_FOLDER", "Shared")

CONTEXT_GROUNDING_INDEX = os.getenv("CONTEXT_GROUNDING_INDEX", "expense-policy-index")

#Initialize the UiPath SDK client
sdk = UiPath()

# Configure UiPath OpenAI client for agent execution
# This routes all OpenAI API calls through UiPath's LLM Gateway
MODEL = OpenAIModels.gpt_4_1_2025_04_14
uipath_openai_client = UiPathChatOpenAI(model_name=MODEL)
_openai_shared.set_default_openai_client(uipath_openai_client.async_client)

#Agent's output will be validated against this schema
class AgentOutput(BaseModel):
    expense_id: str
    amount: float
    category: str
    date: str
    within_policy: bool
    policy_limit: float
    flag_reason: Optional[str]
    recommendation: str


# ─────────────────────────────────────────────────────────────
# 1.  Tool: download_receipt
#     Given to ExpenseParserAgent.
# ─────────────────────────────────────────────────────────────

@function_tool
async def download_receipt(filename: str) -> str:
    """
    Download an expense receipt file from UiPath Storage Bucket.

    Args:
        filename: The name of the file to download, e.g. 'EXP-2025-1142.pdf'.

    Returns:
        The raw content of the receipt file as a string.
    """

    sdk.buckets.download(name=STORAGE_BUCKET_NAME, folder_path=STORAGE_BUCKET_FOLDER, blob_file_path=filename, destination_path=f"./{filename}")
    
    reader = PdfReader(f"./{filename}")

    text = ""

    for page in reader.pages:
        text += page.extract_text() or ""

    try:
        os.remove(f"./{filename}")
    except:
        pass

    return text.strip()


# ─────────────────────────────────────────────────────────────
# 2.  Tool: retrieve_policy
#     Given to PolicyValidatorAgent.
# ─────────────────────────────────────────────────────────────

@function_tool
async def retrieve_policy(query: str) -> str:
    """
    Retrieve relevant expense policy sections from UiPath Context Grounding
    using semantic search.

    Args:
        query: A natural-language question describing the policy information
               needed. Be specific about the expense category and what you
               want to check — e.g. 'travel airfare reimbursement limit per trip'
               or 'client entertainment meal per-person cap'.

    Returns:
        The top matching policy text chunks from the Context Grounding index.
    """

    results = sdk.context_grounding.search(
    name=CONTEXT_GROUNDING_INDEX,
    folder_path="Shared",
    query=query,
    number_of_results=5
    )

    full_text = "\n\n".join(item.content for item in results)
    
    return full_text


# ─────────────────────────────────────────────────────────────
# 3.  Sub-agent: ExpenseParserAgent
#     Has: download_receipt tool
#     Job: fetch the receipt file, extract all fields
# ─────────────────────────────────────────────────────────────

expense_parser_agent = Agent(
    name="ExpenseParserAgent",
    model=MODEL,
    instructions="""
                You are a specialist expense receipt parser.

                When given an filename:
                1. Call download_receipt(filename) to fetch the file from the
                UiPath Storage Bucket.
                2. Parse the returned content and extract all receipt fields.

                Return ONLY a JSON object — no markdown, no explanation:
                {
                "date":             string (ISO 8601),
                "category":         string,
                "line_items":       [{"description": string, "amount": number}],
                "total_amount":     number
                }
                """,
    tools=[download_receipt],
)


# ─────────────────────────────────────────────────────────────
# 4.  Sub-agent: PolicyValidatorAgent
#     Has: retrieve_policy tool
#     Job: query Context Grounding, then validate the expense
# ─────────────────────────────────────────────────────────────

policy_validator_agent = Agent(
    name="PolicyValidatorAgent",
    model=MODEL,
    instructions="""
                You are a specialist expense policy compliance checker.

                When given a parsed expense (JSON):
                1. Call retrieve_policy with a targeted query based on the expense's category
                to fetch relevant sections from UiPath Context Grounding.
                Make the query specific, for example:
                    "travel airfare reimbursement limit receipt requirement"
                    "client entertainment meal per-person cap guest list"
                    "ground transport taxi ride-hailing limit receipt"
                2. Compare every field in the expense against the retrieved policy.
                3. Check BOTH: (a) amount vs. the applicable limit, (b) receipt presence.

                Return ONLY a JSON object — no markdown, no explanation:
                {
                "within_policy": boolean,
                "policy_limit":  number,
                "violations":    [string],
                "notes":         string
                }
                """,
    tools=[retrieve_policy],
)


# ─────────────────────────────────────────────────────────────
# 5.  Sub-agent: ExpenseProcessorAgent
#     No external tools — pure reasoning
#     Job: merge parsed + validation results into a final record
# ─────────────────────────────────────────────────────────────

expense_processor_agent = Agent(
    name="ExpenseProcessorAgent",
    model=MODEL,
    instructions="""
                You are a specialist expense record builder.

                You receive the parsed expense JSON, the policy validation JSON, and a
                filename. Assemble the final structured expense record.

                Rules:
                - recommendation = "auto_approve"    when within_policy=true
                - recommendation = "flag_for_review" in all other cases
                - flag_reason = null when auto-approving
                - flag_reason = a clear, human-readable explanation when flagging
                - expense_id   = "EXP-{filename}" (if not already prefixed)

                Return ONLY a JSON object — no markdown, no explanation:
                {
                "expense_id":      string,
                "amount":          number,
                "category":        string,
                "date":            string,
                "within_policy":   boolean,
                "policy_limit":    number,
                "flag_reason":     string | null,
                "recommendation":  "auto_approve" | "flag_for_review"
                }
                """,
)


# ─────────────────────────────────────────────────────────────
# 6.  Expose sub-agents as tools  (Agent.as_tool)
#     This is the heart of the orchestrator-sub-agent pattern.
#     The orchestrator calls these like regular function tools.
#     Control always returns to the orchestrator — no handoffs.
# ─────────────────────────────────────────────────────────────

parse_expense_tool = expense_parser_agent.as_tool(
    tool_name="parse_expense",
    tool_description=(
        "Download and parse an expense receipt from the UiPath Storage Bucket. "
        "Input: filename string (e.g. 'EXP-2025-1142.pdf'). "
        "Returns structured JSON: date, category, line_items, "
        "total_amount."
    ),
)

validate_policy_tool = policy_validator_agent.as_tool(
    tool_name="validate_policy",
    tool_description=(
        "Validate a parsed expense against the company policy by querying "
        "UiPath Context Grounding. "
        "Input: the full parsed expense JSON string (output of parse_expense). "
        "Returns within_policy, policy_limit, violations list, and notes."
    ),
)

build_expense_record_tool = expense_processor_agent.as_tool(
    tool_name="build_expense_record",
    tool_description=(
        "Assemble the final structured ExpenseRecord from parse + validate results. "
        "Input: combined message containing parsed expense JSON, policy validation "
        "JSON, and the filename. "
        "Returns a complete record with recommendation: auto_approve or flag_for_review."
    ),
)


# ─────────────────────────────────────────────────────────────
# 7.  OrchestratorAgent
#     Owns the conversation. Calls sub-agents as tools in sequence.
#     Never hands off — always synthesises and returns the final answer.
# ─────────────────────────────────────────────────────────────

agent = Agent(
    name="orchestrator",
    model=MODEL,
    instructions="""
                You are the Expense Approval Orchestrator for ACME Corp.

                You coordinate a multi-agent pipeline using specialist tool-agents.
                You own the conversation at all times. You never hand off — you call
                tools and return the final answer yourself.

                PIPELINE — always follow these steps in order:

                Step 1 — Parse the receipt
                Call parse_expense with the filename provided by the user.
                The agent will download the file from UiPath Storage Bucket and
                return a structured JSON of all receipt fields.

                Step 2 — Validate against policy
                Call validate_policy, passing the full JSON string from Step 1.
                The agent will query UiPath Context Grounding for relevant policy
                sections and return a compliance assessment.

                Step 3 — Build the expense record
                Call build_expense_record with a single message containing:
                    PARSED EXPENSE:
                    {output from step 1}

                    POLICY RESULT:
                    {output from step 2}

                    REQUEST ID: {expense_id}

                The agent will return the final structured ExpenseRecord JSON.

                Final output
                Return ONLY the raw JSON object from Step 3.
                No extra text, no markdown code fences.
                """,
    tools=[
        parse_expense_tool,
        validate_policy_tool,
        build_expense_record_tool,
    ],

    output_type=AgentOutput,
)

