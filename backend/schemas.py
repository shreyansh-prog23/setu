"""
Pydantic schemas for the TomTom-backed truck routing endpoint.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator


class CongestionLevel(str, Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    SEVERE = "SEVERE"


class Coordinate(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class RouteRequest(BaseModel):
    origin: Coordinate
    destination: Coordinate
    avoid_unpaved: bool = Field(
        default=False,
        description="Avoid unpaved/unsealed roads - useful for heavy freight trucks in hill terrain.",
    )
    depart_at: Optional[str] = Field(
        default=None,
        description=(
            "ISO 8601 departure time for predictive traffic, e.g. "
            "'2026-08-28T14:30:00+05:30'. Omitted = depart now, with live traffic."
        ),
    )
    hazard_avoid_coords: Optional[List[Coordinate]] = Field(
        default=None,
        description=(
            "Known terrain hazard points (landslide, flood, blockage, etc). The "
            "route is checked against these and automatically rerouted to a "
            "TomTom alternative if the primary corridor passes within 20km of one."
        ),
    )

    @field_validator("depart_at")
    @classmethod
    def _validate_depart_at(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("depart_at must be an ISO 8601 datetime string") from exc
        return value


class RiskSegment(BaseModel):
    km_from_origin: float
    fraction: float = Field(ge=0, le=1, description="0-1 position along the route where the risk concentrates.")


class HazardScore(BaseModel):
    ai_safety_score: float = Field(ge=0, le=100)
    ai_risk_level: str
    risk_factors: List[str] = Field(default_factory=list)
    risk_segment: Optional[RiskSegment] = Field(
        default=None,
        description="Where along the route this hazard's risk concentrates, if localizable - "
        "None means the risk is assessed corridor-wide, not a specific stretch.",
    )


class HazardBreakdown(BaseModel):
    landslide: HazardScore
    earthquake: HazardScore
    flood: HazardScore
    cyclone: HazardScore


class RouteResponse(BaseModel):
    distance_km: float
    travel_time_minutes: float
    traffic_delay_minutes: float
    congestion_level: CongestionLevel
    coordinates: List[Tuple[float, float]] = Field(
        ..., description="Ordered (lat, lon) pairs describing the chosen route path."
    )
    hazard_detected: bool = Field(
        default=False, description="True if the primary corridor intersected a known hazard zone."
    )
    rerouted: bool = Field(
        default=False, description="True if a hazard-clear TomTom alternative was selected instead of the primary route."
    )
    ai_safety_score: float = Field(
        default=0.0, ge=0, le=100,
        description="Unified corridor safety score, 0-100% - the minimum across all 4 hazard models "
        "(landslide, earthquake, flood, cyclone; see multi_hazard.py). A corridor is only as safe as its worst hazard.",
    )
    ai_risk_level: str = Field(
        default="SAFE", description="Risk class (SAFE/MODERATE/HIGH_*) of whichever hazard drove ai_safety_score."
    )
    risk_factors: List[str] = Field(
        default_factory=list, description="Top human-readable feature(s) driving the primary hazard's risk score."
    )
    risk_segment: Optional[RiskSegment] = Field(
        default=None,
        description="Where along the route the primary hazard's risk concentrates, if localizable "
        "(currently only landslide) - None means treat the risk as corridor-wide, not a specific stretch.",
    )
    primary_hazard: str = Field(
        default="landslide", description="Which of the 4 hazards produced ai_safety_score - 'landslide', 'earthquake', 'flood', or 'cyclone'."
    )
    hazard_breakdown: Optional[HazardBreakdown] = Field(
        default=None, description="Per-hazard score/level/factors for all 4 hazards, for a UI 'view details' expansion."
    )
    ai_route_label: Optional[str] = Field(
        default=None,
        description="Set to 'AI Selected: Safest Corridor' when the model chose a different, "
        "safer-scored candidate over TomTom's default-ranked route.",
    )
    elevation_profile: List[float] = Field(
        default_factory=list,
        description="Real elevations (meters) sampled along the chosen route via Open-Meteo, for a UI sparkline.",
    )
    max_gradient_pct: float = Field(
        default=0.0, description="Steepest grade (%) between consecutive sampled points on the chosen route."
    )
    steepest_segment_index: Optional[int] = Field(
        default=None,
        description="Index into elevation_profile where the steepest climb starts (between this point and the "
        "next) - lets the UI highlight exactly where the risky stretch is, not just show one number for the "
        "whole corridor.",
    )

    def to_geojson_feature(self) -> dict:
        """
        Convert to a standard GeoJSON Feature<LineString> for direct use with
        Mapbox GL JS / Leaflet (e.g. as a single-feature source, or wrapped in
        a FeatureCollection alongside other layers).

        Note: GeoJSON coordinate order is [lon, lat] - the reverse of the
        (lat, lon) tuples stored on this model.
        """
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[lon, lat] for lat, lon in self.coordinates],
            },
            "properties": {
                "distance_km": self.distance_km,
                "travel_time_minutes": self.travel_time_minutes,
                "traffic_delay_minutes": self.traffic_delay_minutes,
                "congestion_level": self.congestion_level.value,
                "hazard_detected": self.hazard_detected,
                "rerouted": self.rerouted,
                "ai_safety_score": self.ai_safety_score,
                "ai_risk_level": self.ai_risk_level,
                "risk_factors": self.risk_factors,
                "risk_segment": self.risk_segment.model_dump() if self.risk_segment else None,
                "primary_hazard": self.primary_hazard,
                "hazard_breakdown": self.hazard_breakdown.model_dump() if self.hazard_breakdown else None,
                "ai_route_label": self.ai_route_label,
                "elevation_profile": self.elevation_profile,
                "max_gradient_pct": self.max_gradient_pct,
                "steepest_segment_index": self.steepest_segment_index,
            },
        }
