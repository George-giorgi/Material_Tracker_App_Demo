# BRUSNA Material Tracker — Demo Video Script

**Length:** about 3–4 minutes  
**Language:** English  
**Environment:** isolated demo at `http://127.0.0.1:8001`  
**Data:** fictional quantities, house details, and employees only

The demo app has a built-in **Presentation guide**. Click it to see the
narration, on-screen action, and prefilled form for each scene. Review each
form and click its Save button yourself; this demonstrates the real workflow.
After each action, return to the guide and click **Next**.

On a freshly seeded database, use house ID `DEMO-SHOW-301`. The guide chooses
the next unused `DEMO-SHOW-*` ID automatically if that example already exists.
Do not reset or use the live tracker database to prepare this recording.

## Recording outline

### 1. Introduce the tracker — Overview

**Show:** Overview and the demo-data warning.

**Say:** “This is the BRUSNA material tracker demo. It gives us one place to
see warehouse stock, follow material to a house or team, and keep a record of
who was responsible. The quantities and people shown here are fictional.”

### 2. Add a house

**Action:** Open **Presentation guide** → **Next** → **Open and prefill house
form**. Review the form, then click **Save house**.

**Example details:** `DEMO-SHOW-301`, owner `Riley Example`, employee
`Jordan Demo`, postcode `D01 DEMO`.

**Say:** “First we add the house and record its owner and assigned employee.
That gives later movements a clear destination and makes the house easy to
find.”

### 3. Record the first pickup

**Action:** In the guide choose **Next** → **Open and prefill pickup**. The
form contains `Board 100mm`, 12 units, the example house, `Jordan Demo`, and
`Demo Retrofit Crew`. Review and click **Save movement**.

**Say:** “The team collects twelve boards from the warehouse. Saving this
movement reduces warehouse stock and records those boards at the house, with
the responsible person and team.”

### 4. Record an extra delivery

**Action:** In the guide choose **Next** → **Open and prefill extra delivery**.
Confirm **Mark as an extra delivery** is checked, then save four more boards.

**Say:** “The house needs more material, so we record a later pickup and mark
it as an extra delivery. The tracker numbers it after the first delivery and
highlights it in the movement history.”

### 5. Return unused material

**Action:** In the guide choose **Next** → **Open and prefill return**. Review
the return of two boards to the warehouse and save.

**Say:** “Two unused boards come back from site. Recording the return updates
both the house balance and the warehouse balance, instead of losing track of
the leftover material.”

### 6. Transfer to another site

**Action:** In the guide choose **Next** → **Open and prefill transfer**.
Review the source house, destination `DEMO-202`, five-board quantity, person,
and team; then save.

**Say:** “If materials move to another job, we record both locations. The
tracker removes the quantity from the source and adds it to the destination,
while retaining the transfer in the ledger.”

### 7. Review stock and finish the house

**Action:** In the guide choose **Next** → **Show updated inventory**. Then
choose **Next** → **Show the house card** and click **Mark house finished**
on the example house. The guide minimizes so it does not cover the card;
click **Resume presentation** afterward.

**Say:** “Inventory shows the revised balances after each action. When the
work is complete, we mark the house finished. It stops counting as active,
but its remaining stock and movement history stay available.”

### 8. Explore houses, teams, and checklists

**Action:** In the guide choose **Next** → **Show houses & teams**. Search for
`DEMO-SHOW-301`, review its checklist and assigned team on the house card, then
try the team's open/closed house count to filter the houses.

**Say:** “Houses & teams gives us a quick view of active and closed jobs, their
checklist progress, and the teams connected through recorded movements. We can
search houses by ID, owner, employee, or postcode, or find a team by name.
Selecting a team's open or closed count shows the matching houses.
Administrators can update the checklist, while employees can view it.”

**Note:** Team house counts are based on recorded movements. Creating a house
without recording a movement does not assign it to a team.

### 9. Search the house history

**Action:** In the guide choose **Next** → **Show house movement history**.

**Say:** “Finally, we search by house ID and see the matching pickups, extra
delivery, return, and site transfer together, including their dates and
responsibility details.”

### 10. Close

**Show:** The demo warning and the completed house movement history.

**Say:** “This demo shows how the tracker can make material movement easier
to see and reconcile. This is a local-first prototype; its sample data is
fictional and is not the company’s live stock.”

## Before recording

- Start the isolated demo and confirm the **DEMO DATA — NOT REAL STOCK** banner
  is visible.
- On macOS, open QuickTime Player, choose **File → New Screen Recording**,
  select the microphone if recording narration live, and record the browser
  window showing port `8001`.
- Use the built-in presentation guide so the matching forms are prefilled.
- Save each movement before moving to the next scene.
- Keep the browser on the demo port `8001`; do not record the live tracker on
  port `8000` or expose real house/employee information.
- If repeating the demo, the guide chooses the next unused demonstration
  house ID; check the delivery number shown in the form before saving.
