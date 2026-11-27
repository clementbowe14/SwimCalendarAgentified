# Google Calendar checks when confirming requests

Both provider confirmation routes must check the connected calendar against the lesson being confirmed. A weekly request must pass the check for every occurrence. An overlap returns the existing scheduling conflict. An unverified calendar returns the existing calendar availability error. Neither condition creates appointments or queues a confirmation.

Read Google before taking confirmation locks. Inside the existing transaction, re-read the request and validate the schedule with the verified busy periods. Check the calendar connection generation under lock before committing so a reconnect cannot change which calendar was checked. A committed confirmation remains safe to replay without Google.

Cover busy, available, unavailable, weekly, and replay paths using scoped fake Google responses and the PostgreSQL integration fixture.
