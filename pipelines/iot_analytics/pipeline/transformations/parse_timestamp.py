import typing
import datetime
from apache_beam.transforms.window import TimestampedValue
import logging

# input {"vehicle_id":"1009","timestamp":"2024-09-14T14:17:43Z","temperature":72,"rpm":2554,"vibration":0.25,"fuel_level":82,"mileage":59672}
class VehicleStateEvent(typing.NamedTuple):
    vehicle_id: str
    timestamp: datetime.datetime
    temperature: int
    rpm: int
    vibration: float
    fuel_level: int
    mileage: int

    @staticmethod
    def convert_json_to_vehicleobj(input_json):
        dt_object = datetime.datetime.strptime(input_json['timestamp'], '%Y-%m-%dT%H:%M:%SZ') 
        # dt_time = dt_object.strftime('%Y-%m-%dT%H:%M:%SZ') 
        event = VehicleStateEvent(
            vehicle_id=input_json['vehicle_id'],
            timestamp= dt_object,
            temperature=input_json['temperature'],
            rpm= input_json['rpm'],
            vibration= input_json['vibration'],
            fuel_level= input_json['fuel_level'],
            mileage= input_json['mileage']
        )
        logging.info(f"Parse Timestamp : {event}")
        return TimestampedValue(event, dt_object.timestamp())