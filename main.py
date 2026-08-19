import sys
from app.db import init_db, get_instructor_info, get_appointments_for_date
from app.agent import SwimSchedulingAgent

def main():
    print("=" * 70)
    print(" 🏊 AGENTIC SWIM CLIENT SCHEDULING SYSTEM (MVP)")
    print("=" * 70)

    # Initialize DB & Seed Data
    init_db()
    instructor = get_instructor_info("inst_001")
    print(f"Instructor Base: {instructor.name} @ {instructor.main_address}")
    print(f"Working Hours: {instructor.work_start_time} - {instructor.work_end_time}")

    test_date = "2026-08-20"
    print(f"\nExisting Appointments & Breaks for {test_date}:")
    events = get_appointments_for_date("inst_001", test_date)
    for ev in events:
        tag = "[SCHEDULED BREAK]" if ev["is_break"] else "[LESSON]"
        print(f" - {tag} {ev['title']} ({ev['start_time'].strftime('%I:%M %p')} - {ev['end_time'].strftime('%I:%M %p')}) @ {ev['location_address']}")

    print("\n" + "-" * 70)
    print(" SCENARIO 1: Client 'Charlie' requests In-Home Lesson at North Address")
    print("-" * 70)

    agent = SwimSchedulingAgent(instructor_id="inst_001")
    state1 = agent.process_request(
        client_name="Charlie",
        client_address="450 North Blvd, Austin, TX",
        requested_date=test_date,
        is_instructor_facility=False,
        duration_mins=60
    )

    print("\nAgent Response:")
    print(state1["dialogue_response"])

    if state1["available_slots"]:
        print("\n--> Client Charlie selects Slot 1...")
        state1_booked = agent.confirm_booking(state1, slot_index=0)
        print("\nAgent Booking Confirmation:")
        print(state1_booked["dialogue_response"])

    print("\n" + "-" * 70)
    print(" SCENARIO 2: Client 'Diana' requests Lesson at Instructor's Main Address")
    print("-" * 70)

    state2 = agent.process_request(
        client_name="Diana",
        client_address="Instructor Facility",
        requested_date=test_date,
        is_instructor_facility=True,
        duration_mins=60
    )

    print("\nAgent Response:")
    print(state2["dialogue_response"])

    print("\n" + "=" * 70)
    print(" Verification complete!")

if __name__ == "__main__":
    main()
