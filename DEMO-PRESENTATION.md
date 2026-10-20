# BRUSNA Material Tracker — Boss Demo Guide

**Length:** about 4–5 minutes
**Language:** English  
**Environment:** isolated demo at `http://127.0.0.1:8001`  
**Data:** fictional house details, people, and quantities

Use the in-app **Presentation guide** for matching narration and shortcuts.
When it minimizes, complete the requested action on the house card or form,
then click **Resume presentation**.

The demo contains 50 provided houses, all starting as **Not started**. Moving a
house through Ready for start, Active, and Closed does not add or remove a
provided-house record. House details keep notes, survey notes, snags, and
material movement history together.

## Recording outline

### 1. Introduce the house workflow

**Show:** Overview house-count tiles and the Not started house cards.

**Say:** “The tracker keeps one list of 50 provided houses. A clear workflow
shows which jobs have not started, are ready for a survey, are active, or are
closed.”

### 2. Review a house not started

**Action:** Open **Houses & teams** and select **Not started**. Show the
workflow progress on a house card, then use **Mark ready for start**.

**Say:** “Each card shows its place in the workflow. Stage 1 and Stage 2 are
work stages and appear after the house is activated.”

### 3. Save and review a survey

**Action:** On the Ready for start card, enter a small fictional example, such
as one window at 1.2 m by 1.5 m, one door at 0.9 m by 2 m, and a corner length.
Save the survey, then review it.

**Say:** “Survey dimensions are saved against this house. The calculator can
load them automatically, and window corner lengths include the width plus
both sides.”

### 4. Activate the surveyed house and show its stage

**Action:** On the same card, click **+ Activate house** and save. In **Active
houses**, select Stage 2 to show the green selected state; Stage 1 is blue.

**Say:** “After review, the house moves into active work. Its current stage,
checklist, materials, notes, and snags are visible together.”

### 5. Record material movements

Follow the in-app guide to prefill and save a warehouse pickup, an extra
delivery, a return, and a transfer from the house to **Demo Retrofit Crew**.
Review each form and click **Save movement**.

**Say:** “Each movement records its source, destination, quantity, and who is
responsible. The house and warehouse balances update together, and the ledger
keeps the delivery and return history.”

### 6. Review balances and finish the work

**Action:** Show updated inventory, then return to the active house and click
**Mark house finished**.

**Say:** “When work is complete, finishing the house moves it from Active to
Closed. The system retains its records and shows any remaining balance.”

### 7. Review house notes, snags, and movement history

**Action:** Open **View details & history** on a house card.

**Say:** “The detail view brings house and survey notes, open and completed
snag history, and the complete material movement history together in one
place.”

### 8. Close

**Show:** The house details and history view.

**Say:** “This demo shows the path from survey to activation, material
accountability, completion, and retained house history. It is a local-first
prototype using fictional demo data, not the company’s live stock.”

## Before recording

- Start the isolated demo and confirm the **DEMO DATA — NOT REAL STOCK** banner
  is visible.
- Keep the browser on demo port `8001`; do not record the live tracker on port
  `8000` or expose real house or employee information.
- Use the existing provided houses; the pool should remain at 50.
- Save each survey and movement you intend to demonstrate. These actions
  change the demo database and remain there after recording.
- The guide's movement forms use fictional people and quantities. Review the
  destination and quantity before saving each form.
