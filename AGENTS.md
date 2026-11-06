# Workspace Instructions

## “Back stage 10” shortcut

When the user says “back stage 10,” reset every house currently marked Ready for start to Not started using the application's existing reset behavior. Target only provided houses that are still marked ready and are not active. Clear the associated survey, snag, checklist, notes, work-plan, and annotated-PDF records exactly as `_clear_house_survey_records` does, and record a `not_started` status-history entry for each changed house. Resolve and verify the database used by the running app before changing data; never guess the database path. If no houses qualify, make no changes and report that. Afterward verify the remaining ready count and report how many houses changed.
