# Session Feedback API

All endpoints require a Clerk bearer token. Feedback is always checked against
the authenticated user's `interview_sessions.user_id`; a client cannot submit
feedback for another user's session.

## Submit or update feedback

`POST /api/feedback`

```json
{
  "session_id": "4e75fb46-23aa-4ec1-92aa-746a174888c1",
  "source": "report",
  "emotion": "good",
  "rating": 4,
  "comment": "The report was useful, but one recommendation was inaccurate.",
  "tags": ["useful", "incorrect_feedback"]
}
```

Allowed values:

- `source`: `interview`, `coding`, `report`
- `emotion`: `very_bad`, `bad`, `neutral`, `good`, `great`
- `rating`: integer from 1 to 5
- `tags`: `too_hard`, `too_easy`, `audio_issue`, `useful`,
  `incorrect_feedback`

The operation is an upsert on `(session_id, user_id, source)`, so a user may
revise feedback for the same stage without creating duplicates.

## Create a completed coding session

`POST /api/feedback/coding-sessions`

Coding currently runs client-side and previously had no database session. Call
this endpoint once when coding analysis finishes, then use the returned `id` as
`session_id` when submitting coding feedback.

```json
{
  "position": "Backend Engineer",
  "company": "VieCareer",
  "duration_seconds": 1200,
  "overall_score": 82
}
```

## Admin feedback list

`GET /api/admin/feedback`

Supported query parameters:

- `negative_only=true`: emotion is `very_bad`/`bad`, or rating is 1-2
- `source`, `emotion`, `rating_max`, `tag`
- `search`: user, email, position, comment, or session ID
- `limit`, `offset`

Example:

`GET /api/admin/feedback?negative_only=true&source=report&limit=25`

Interactive OpenAPI documentation is also available from FastAPI at `/docs`.
