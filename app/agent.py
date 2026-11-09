from typing import TypedDict, List, Optional
import datetime
from app import tools
from app.models import TimeSlotDTO
from app.provider_settings import require_provider_setup

class AgentState(TypedDict):
    messages: List[dict]
    client_name: Optional[str]
    client_address: Optional[str]
    is_instructor_facility: bool
    requested_date: Optional[str]
    duration_mins: int
    available_slots: List[dict]
    selected_slot: Optional[dict]
    booking_confirmation: Optional[dict]
    status: str # "COLLECTING", "OPTIMIZING", "PRESENTING", "BOOKED", "FAILED"
    dialogue_response: str

class SwimSchedulingAgent:
    """
    LangGraph State Machine Agent orchestrator.
    Combines intake extraction, deterministic constraint solving, and dialogue presentation.
    """
    def __init__(self, instructor_id: str = "inst_001"):
        self.instructor_id = instructor_id

    def process_request(
        self,
        client_name: str,
        client_address: str,
        requested_date: str, # YYYY-MM-DD
        is_instructor_facility: bool = False,
        duration_mins: int = 60
    ) -> AgentState:
        state: AgentState = {
            "messages": [],
            "client_name": client_name,
            "client_address": client_address,
            "is_instructor_facility": is_instructor_facility,
            "requested_date": requested_date,
            "duration_mins": duration_mins,
            "available_slots": [],
            "selected_slot": None,
            "booking_confirmation": None,
            "status": "OPTIMIZING",
            "dialogue_response": ""
        }

        # Step 1: Execute Constraint Engine Node
        try:
            raw_slots = tools.find_available_swim_slots(
                instructor_id=self.instructor_id,
                date_str=requested_date,
                client_address=client_address,
                is_instructor_facility=is_instructor_facility,
                duration_mins=duration_mins
            )

            state["available_slots"] = [
                {
                    "start_iso": s.start_time.isoformat(),
                    "end_iso": s.end_time.isoformat(),
                    "display": s.format_display(),
                    "travel_time_mins": s.travel_time_mins,
                    "buffer_mins": s.buffer_mins,
                    "distance_miles": s.distance_miles
                }
                for s in raw_slots
            ]
            state["status"] = "PRESENTING"

            # Format dialogue response
            provider = require_provider_setup(self.instructor_id)
            location_str = (
                provider.service_location.address
                if is_instructor_facility and provider.service_location is not None
                else client_address
            )

            if not raw_slots:
                state["dialogue_response"] = (
                    f"Hello {client_name}, I checked {provider.display_name}'s schedule for {requested_date} at {location_str}. "
                    f"Unfortunately, due to distance travel buffers and existing appointments, there are no open slots available on this date. "
                    f"Would you like to check an alternative date?"
                )
            else:
                slot_lines = [f"  {idx+1}. {s['display']}" for idx, s in enumerate(state["available_slots"])]
                slots_formatted = "\n".join(slot_lines)
                state["dialogue_response"] = (
                    f"Hello {client_name}! Here are the mathematically calculated, available swim lesson times for "
                    f"{requested_date} at '{location_str}' (accounting for {provider.display_name}'s schedule & travel buffers):\n\n"
                    f"{slots_formatted}\n\n"
                    f"Please confirm which slot number you'd like to book!"
                )
        except Exception as e:
            state["status"] = "FAILED"
            state["dialogue_response"] = f"An error occurred while evaluating constraints: {str(e)}"

        return state

    def confirm_booking(self, state: AgentState, slot_index: int) -> AgentState:
        """
        Executes booking node for the chosen slot index.
        """
        if not state["available_slots"] or slot_index < 0 or slot_index >= len(state["available_slots"]):
            state["dialogue_response"] = "Invalid slot choice. Please select a valid slot number from the options presented."
            return state

        chosen = state["available_slots"][slot_index]
        state["selected_slot"] = chosen

        location_str = "Instructor Facility" if state["is_instructor_facility"] else state["client_address"]

        booking_result = tools.book_swim_lesson(
            instructor_id=self.instructor_id,
            client_id=f"client_{state['client_name'].lower().replace(' ', '_')}",
            client_name=state["client_name"],
            start_time_iso=chosen["start_iso"],
            duration_mins=state["duration_mins"],
            location_address=location_str,
            is_instructor_facility=state["is_instructor_facility"],
        )

        state["booking_confirmation"] = booking_result
        state["status"] = "BOOKED"
        state["dialogue_response"] = (
            f"🎉 {booking_result['message']}\n"
            f"Appointment ID: {booking_result['appointment_id']}\n"
            f"Travel time: {chosen['travel_time_mins']} mins | Buffer: {chosen['buffer_mins']} mins | Distance: {chosen['distance_miles']} mi."
        )

        return state
