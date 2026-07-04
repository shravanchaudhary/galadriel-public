import asyncio
import logging
from typing import Dict, List, Optional, NamedTuple
from .fullenrich_client import (
    FullEnrichClient,
    FullEnrichContact,
    FullEnrichRequest,
)

# Explorium contacts enrichment costs 5 credits per prospect
_EXPLORIUM_CONTACTS_CREDITS = 5

logger = logging.getLogger(__name__)


class ContactInfo(NamedTuple):
    """Input contact information for enrichment"""

    firstname: str
    lastname: str
    company_name: Optional[str] = None
    domain: Optional[str] = None
    linkedin_url: Optional[str] = None


class EnrichmentResult(NamedTuple):
    """Result of contact enrichment"""

    firstname: str
    lastname: str
    email: Optional[str] = None
    phone: Optional[str] = None
    email_verification_status: Optional[str] = None
    linkedin_url: Optional[str] = None
    company_name: Optional[str] = None
    domain: Optional[str] = None
    error: Optional[str] = None


class FullEnrichService:
    def __init__(self, api_key: str = None):
        self.client = FullEnrichClient(api_key)

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.__aexit__(exc_type, exc_val, exc_tb)

    async def full_enrich(
        self, contacts: List[ContactInfo], fields: List[str] = ["contact.emails"]
    ) -> List[EnrichmentResult]:
        """
        Enrich a list of contacts with email, phone, and verification status.

        Args:
            contacts: List of ContactInfo objects with firstname, lastname, and optionally
                     company_name, domain, and linkedin_url

        Returns:
            List of EnrichmentResult objects with enriched data
        """
        if not contacts:
            return []

        if len(contacts) > 100:
            raise ValueError("Maximum 100 contacts can be enriched at once")

        # Validate input contacts
        for contact in contacts:
            if not contact.firstname or not contact.lastname:
                raise ValueError("firstname and lastname are required for all contacts")
            if not contact.domain and not contact.company_name:
                raise ValueError(
                    "Either domain or company_name must be provided for all contacts"
                )

        try:
            # Prepare enrichment request
            enrich_contacts = []
            for i, contact in enumerate(contacts):
                enrich_contact = FullEnrichContact(
                    firstname=contact.firstname,
                    lastname=contact.lastname,
                    domain=contact.domain,
                    company_name=contact.company_name,
                    linkedin_url=contact.linkedin_url,
                    enrich_fields=fields,
                    custom={"index": str(i)},  # Track original position
                )
                enrich_contacts.append(enrich_contact)

            # Create enrichment request
            enrichment_name = f"Bulk Enrichment - {len(contacts)} contacts"
            request = FullEnrichRequest(name=enrichment_name, datas=enrich_contacts)

            # Start enrichment
            logger.info(f"Starting enrichment for {len(contacts)} contacts")
            response = await self.client.start_bulk_enrichment(request)

            # Wait for completion
            logger.info(f"Waiting for enrichment {response.enrichment_id} to complete")
            result = await self.client.wait_for_completion(response.enrichment_id)

            # Process results
            enriched_results = []

            if result.status == "FINISHED":
                # Create a mapping from original index to result
                result_map = {}
                for data in result.datas:
                    if "custom" in data and "index" in data["custom"]:
                        index = int(data["custom"]["index"])
                        result_map[index] = data

                # Build results in original order
                for i, original_contact in enumerate(contacts):
                    if i in result_map:
                        data = result_map[i]
                        contact_data = data.get("contact", {})

                        # Extract enriched information
                        email = contact_data.get("most_probable_email")
                        phone = contact_data.get("most_probable_phone")
                        email_status = contact_data.get("most_probable_email_status")

                        # Extract LinkedIn URL if not provided originally
                        linkedin_url = original_contact.linkedin_url
                        if not linkedin_url and "social_medias" in contact_data:
                            for social in contact_data["social_medias"]:
                                if social.get("type") == "LINKEDIN":
                                    linkedin_url = social.get("url")
                                    break

                        # Extract company info from profile if available
                        company_name = original_contact.company_name
                        domain = original_contact.domain
                        if (
                            "profile" in contact_data
                            and "position" in contact_data["profile"]
                        ):
                            position = contact_data["profile"]["position"]
                            if "company" in position:
                                company_info = position["company"]
                                if not company_name:
                                    company_name = company_info.get("name")
                                if not domain:
                                    domain = company_info.get("domain")

                        enriched_results.append(
                            EnrichmentResult(
                                firstname=original_contact.firstname,
                                lastname=original_contact.lastname,
                                email=email,
                                phone=phone,
                                email_verification_status=email_status,
                                linkedin_url=linkedin_url,
                                company_name=company_name,
                                domain=domain,
                            )
                        )
                    else:
                        # No result found for this contact
                        enriched_results.append(
                            EnrichmentResult(
                                firstname=original_contact.firstname,
                                lastname=original_contact.lastname,
                                company_name=original_contact.company_name,
                                domain=original_contact.domain,
                                linkedin_url=original_contact.linkedin_url,
                                error="No enrichment data found",
                            )
                        )

            elif result.status == "CREDITS_INSUFFICIENT":
                logger.error("Insufficient credits for enrichment")
                # Return all contacts with error status
                for contact in contacts:
                    enriched_results.append(
                        EnrichmentResult(
                            firstname=contact.firstname,
                            lastname=contact.lastname,
                            company_name=contact.company_name,
                            domain=contact.domain,
                            linkedin_url=contact.linkedin_url,
                            error="Insufficient credits",
                        )
                    )

            elif result.status == "CANCELED":
                logger.error("Enrichment was canceled")
                # Return all contacts with error status
                for contact in contacts:
                    enriched_results.append(
                        EnrichmentResult(
                            firstname=contact.firstname,
                            lastname=contact.lastname,
                            company_name=contact.company_name,
                            domain=contact.domain,
                            linkedin_url=contact.linkedin_url,
                            error="Enrichment was canceled",
                        )
                    )

            else:
                logger.error(f"Enrichment failed with status: {result.status}")
                # Return all contacts with error status
                for contact in contacts:
                    enriched_results.append(
                        EnrichmentResult(
                            firstname=contact.firstname,
                            lastname=contact.lastname,
                            company_name=contact.company_name,
                            domain=contact.domain,
                            linkedin_url=contact.linkedin_url,
                            error=f"Enrichment failed: {result.status}",
                        )
                    )

            logger.info(f"Enrichment completed for {len(enriched_results)} contacts")
            return enriched_results

        except Exception as e:
            logger.error(f"Error during enrichment: {str(e)}")
            # Return all contacts with error status
            error_results = []
            for contact in contacts:
                error_results.append(
                    EnrichmentResult(
                        firstname=contact.firstname,
                        lastname=contact.lastname,
                        company_name=contact.company_name,
                        domain=contact.domain,
                        linkedin_url=contact.linkedin_url,
                        error=str(e),
                    )
                )
            return error_results

    async def enrich_single_contact(
        self, contact: ContactInfo, fields: List[str] = ["contact.emails"]
    ) -> EnrichmentResult:
        """
        Enrich a single contact - convenience method for single contact enrichment
        """
        results = await self.full_enrich([contact], fields)
        return (
            results[0]
            if results
            else EnrichmentResult(
                firstname=contact.firstname,
                lastname=contact.lastname,
                company_name=contact.company_name,
                domain=contact.domain,
                linkedin_url=contact.linkedin_url,
                error="No result returned",
            )
        )

    @classmethod
    async def get_email_and_phone_number(
        cls, firstname, lastname, company_name, domain, linkedin_url
    ):
        contact = ContactInfo(
            firstname=firstname,
            lastname=lastname,
            company_name=company_name,
            domain=domain,
            linkedin_url=linkedin_url,
        )
        async with cls() as service:
            result = await service.enrich_single_contact(
                contact, fields=["contact.emails", "contact.phones"]
            )
            # DELIVERABLE: 2% bounce rate.
            # HIGH_PROBABILITY: 8% bounce rate. (those are emails that are catch-all, but likely valid based on our triple verification process)
            # CATCH_All
            logger.info(
                f"Email verification status: {result.email_verification_status}"
            )
            email_verified = result.email_verification_status in [
                "DELIVERABLE",
                "HIGH_PROBABILITY",
                "CATCH_ALL",
            ]
            logger.info(f"Email verified: {email_verified}")
            return {
                "email": result.email,
                "phone": result.phone,
                "email_verified": email_verified,
            }

    @classmethod
    async def get_email(cls, firstname, lastname, company_name, domain, linkedin_url):
        contact = ContactInfo(
            firstname=firstname,
            lastname=lastname,
            company_name=company_name,
            domain=domain,
            linkedin_url=linkedin_url,
        )
        async with cls() as service:
            result = await service.enrich_single_contact(
                contact, fields=["contact.emails"]
            )
            email_verified = result.email_verification_status in [
                "DELIVERABLE",
                "HIGH_PROBABILITY",
                "CATCH_ALL",
            ]
            return {
                "email": result.email,
                "email_verified": email_verified,
            }

    @classmethod
    async def get_phone(cls, firstname, lastname, company_name, domain, linkedin_url):
        contact = ContactInfo(
            firstname=firstname,
            lastname=lastname,
            company_name=company_name,
            domain=domain,
            linkedin_url=linkedin_url,
        )
        async with cls() as service:
            result = await service.enrich_single_contact(
                contact, fields=["contact.phones"]
            )
            return {
                "phone": result.phone,
            }

    @classmethod
    async def _fetch_contacts_from_explorium(
        cls,
        prospect_id: str,
        org_id: Optional[str] = None,
    ) -> dict:
        """Internal: call Explorium contacts enrichment for one prospect.

        Explorium's ``contacts`` enrichment type (5 credits/prospect) returns
        email and phone together.  Both fields are surfaced in the result so
        callers can opportunistically save whichever they need.

        Returns::

            {
              "email": str | None,
              "phone": str | None,
              "explorium_credits_used": float,
            }

        Email field precedence: ``professions_email`` → first item in ``emails``.
        Phone field precedence: ``mobile_phone`` → first item in ``phone_numbers``.
        """
        from .explorium import ExploriumClient

        client = ExploriumClient()
        resp = await client.bulk_enrich_prospects(
            [prospect_id], "contacts_information", org_id=org_id
        )

        items = resp.get("data") or []
        data: dict = (items[0].get("data") or {}) if items else {}

        email = data.get("professions_email") or next(
            iter(data.get("emails") or []), None
        )
        phone = data.get("mobile_phone") or next(
            iter(data.get("phone_numbers") or []), None
        )
        return {
            "email": email,
            "phone": phone,
            "explorium_credits_used": 0,
        }

    @classmethod
    async def get_email_from_explorium(
        cls,
        prospect_id: str,
        org_id: Optional[str] = None,
    ) -> dict:
        """Fetch email via Explorium contacts enrichment (5 credits/prospect).

        Returns::

            {"email": str | None, "explorium_credits_used": float}
        """
        result = await cls._fetch_contacts_from_explorium(prospect_id, org_id=org_id)
        return {
            "email": result["email"],
            "explorium_credits_used": result["explorium_credits_used"],
        }

    @classmethod
    async def get_phone_from_explorium(
        cls,
        prospect_id: str,
        org_id: Optional[str] = None,
    ) -> dict:
        """Fetch phone via Explorium contacts enrichment (5 credits/prospect).

        Returns::

            {"phone": str | None, "explorium_credits_used": float}
        """
        result = await cls._fetch_contacts_from_explorium(prospect_id, org_id=org_id)
        return {
            "phone": result["phone"],
            "explorium_credits_used": result["explorium_credits_used"],
        }
