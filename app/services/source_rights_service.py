"""Rights evidence from trusted metadata. Lack of a reuse grant is not a ban."""


class SourceRightsService:
    AUTO_ACQUISITION_ALLOWED = frozenset({"OWNED", "LICENSED", "PERMITTED"})

    @classmethod
    def allows_auto_acquisition(cls, rights_status: str) -> bool:
        return rights_status in cls.AUTO_ACQUISITION_ALLOWED

    @classmethod
    def allows_public_acquisition(cls, rights_status: str, *, public_verified: bool) -> bool:
        return public_verified and (rights_status == "UNKNOWN" or
                                    cls.allows_auto_acquisition(rights_status))

    @staticmethod
    def assess_rights(yt_api_video_item: dict, *, owned_channel_id: str = "") -> dict:
        status = yt_api_video_item.get("status") or {}
        snippet = yt_api_video_item.get("snippet") or {}

        if status.get("privacyStatus") == "private":
            return {"rights_status": "RESTRICTED", "rights_reason": "PRIVATE_VIDEO"}

        channel_id = snippet.get("channelId") or ""
        if owned_channel_id and channel_id == owned_channel_id:
            return {"rights_status": "OWNED", "rights_reason": "CONFIGURED_CREATOR_CHANNEL"}

        license_type = status.get("license")
        if license_type == "creativeCommon":
            return {"rights_status": "LICENSED", "rights_reason": "YOUTUBE_CREATIVE_COMMONS"}
        if license_type == "youtube":
            return {"rights_status": "UNKNOWN", "rights_reason": "STANDARD_YOUTUBE_LICENSE"}
        if license_type:
            return {"rights_status": "UNKNOWN", "rights_reason": "UNRECOGNIZED_LICENSE"}
        return {"rights_status": "UNKNOWN", "rights_reason": "LICENSE_NOT_PROVIDED"}

    @classmethod
    def evaluate_rights(cls, yt_api_video_item: dict, *, owned_channel_id: str = "") -> str:
        return cls.assess_rights(
            yt_api_video_item, owned_channel_id=owned_channel_id)["rights_status"]
