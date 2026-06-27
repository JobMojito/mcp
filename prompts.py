"""User-invocable workflow prompts (slash-command style starters).

These wrap the common JobMojito cookbooks so a user can kick off a multi-step
workflow by name. Each prompt seeds the conversation with the goal and the
correct tool sequence, so the model follows the cookbook instead of chaining
tools by trial and error. Prompts are user-triggered; the model still executes
the work through the existing tools.

All prompt arguments are strings (per the MCP spec); optional ones default to "".
"""

from __future__ import annotations


def register(mcp) -> None:
    """Register workflow prompts on the given FastMCP server."""

    @mcp.prompt(tags={"workflow"})
    def create_interview(role: str, location: str = "", language: str = "en") -> str:
        """Start the workflow to create a JobMojito interview for a role."""
        loc = f" in {location}" if location else ""
        return (
            f"Goal: create a JobMojito interview for the role \"{role}\"{loc} "
            f"(language code: {language or 'en'}).\n\n"
            "Follow the 'Create an interview' cookbook:\n"
            "1. If you're unsure about any field, call `search_documentation` first.\n"
            "2. Choose an interview template with `list_avatars` and take an "
            "`interview_template_id`.\n"
            "3. Call `create_interview` with at least: name, location, "
            "interview_template_id, mojito_language_code, status=\"active\", "
            "type=\"interview\", and a visibility (default \"merchant_invite\"). "
            "Ask me for anything required that you don't have rather than guessing.\n"
            "4. Report the new interview id, then offer to generate candidate links "
            "or send invitations (`generate_interview_url` / "
            "`register_users_for_interview`)."
        )

    @mcp.prompt(tags={"workflow"})
    def review_candidate(candidate_or_result: str = "") -> str:
        """Start the workflow to review a candidate's interview result."""
        target = (
            f" The candidate / result to review: \"{candidate_or_result}\"."
            if candidate_or_result
            else ""
        )
        return (
            "Goal: review a candidate's JobMojito interview result." + target + "\n\n"
            "Follow the 'Review results' cookbook:\n"
            "1. Use `list_interview_results` (filter by tab/score or candidate) to "
            "find the right `interview_result_id`. If I named a candidate, search "
            "for them; if I gave a result id, skip to step 2.\n"
            "2. Call `get_interview_result_details` for the transcript, scores, and "
            "the AI recruiter assessment (why-hire / why-not-hire, risks).\n"
            "3. Summarize the outcome for me, then offer to export a report with "
            "`generate_interview_report` (PDF/HTML/JSON)."
        )

    @mcp.prompt(tags={"workflow"})
    def screen_resume(
        position: str,
        candidate_name: str = "",
        candidate_email: str = "",
    ) -> str:
        """Start the workflow to pre-screen a candidate's résumé against a position."""
        who = candidate_name or "the candidate"
        contact = f" ({candidate_email})" if candidate_email else ""
        return (
            f"Goal: pre-screen {who}{contact} against the position \"{position}\".\n\n"
            "Follow the 'Pre-screen candidates' cookbook:\n"
            "1. Identify or create the pre-screening position: if it already exists "
            "use its `position_def_set_id`; otherwise call `upsert_pre_screening` "
            "(position_name, position_location, position_country_code, "
            "mojito_language_code) and use the returned id.\n"
            "2. Ask me for the candidate's résumé text (and country) if I haven't "
            "provided it, then call `pre_screen_resume_text` (or "
            "`pre_screen_resume_binary` for an uploaded file).\n"
            "3. Report the recommendation, score, and résumé analysis, then offer to "
            "invite passing candidates to a full interview."
        )

    @mcp.prompt(tags={"workflow"})
    def invite_candidates(interview_id: str = "", emails: str = "") -> str:
        """Start the workflow to invite candidates to an interview."""
        details = []
        if interview_id:
            details.append(f"interview id: {interview_id}")
        if emails:
            details.append(f"candidates: {emails}")
        ctx = ("\nKnown details — " + "; ".join(details) + ".") if details else ""
        return (
            "Goal: invite candidates to a JobMojito interview." + ctx + "\n\n"
            "Follow the 'Invite candidates' cookbook:\n"
            "1. If I didn't give an interview id, use `list_interviews` to find it.\n"
            "2. Ask me whether to just generate links or also send the email "
            "invitation chain.\n"
            "3. Call `register_users_for_interview` with the interview_id, "
            "type (\"url\" for links or \"invitation\" for emails), hide_menu=false, "
            "and the users (name + email each).\n"
            "4. Return each candidate's interview_url and result, and flag any errors."
        )
