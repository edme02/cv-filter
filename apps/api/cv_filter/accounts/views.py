import hashlib
import os
from pathlib import Path

from django.contrib.auth import authenticate, get_user_model
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from document_extraction.extract_data import CVTextExtractor
from entity_extraction.entity_extractor import CVEntityExtractor
from entity_extraction.models import ExtractedEntities
from summarization.summarizer import CVSummarizer
from .embedding_service import EmbeddingService
from .local_nlq_parser import LocalNLQParser
from .models import CVFile, CVParse, CVParseStatus, Candidate, CandidateStructuredData, SearchDocument, SearchEntityType
from .logging_service import AuditLogService, AuditSeverity, CVAccessEventService
from .serializers import (
    AuditLogSerializer,
    CVAccessEventSerializer,
    CVUploadSerializer,
    CVFileListSerializer,
    CandidateBasicSerializer,
    CandidateCreateSerializer,
    LoginSerializer,
    OrganizationCreateSerializer,
    OrganizationSelectSerializer,
    OrganizationSerializer,
    RegisterSerializer,
    UserUpdateSerializer,
)

User = get_user_model()


class RegisterView(APIView):
    """
    API endpoint to register a user and return JWT tokens.
    """

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                'user': RegisterSerializer(user).data,
                'access': str(refresh.access_token),
                'refresh': str(refresh),
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """
    API endpoint to log in and return JWT tokens.
    """

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            username=serializer.validated_data['username'],
            password=serializer.validated_data['password'],
        )
        if not user:
            return Response(
                {'detail': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED
            )
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                'user': RegisterSerializer(user).data,
                'access': str(refresh.access_token),
                'refresh': str(refresh),
            }
        )


class LoginPageView(TemplateView):
    template_name = 'accounts/login.html'


class RegisterPageView(TemplateView):
    template_name = 'accounts/register.html'


class CVUploadView(APIView):
    """
    API endpoint to upload a CV file for a candidate.
    Handles file validation, storage, and database record creation.
    """

    permission_classes = [IsAuthenticated]

    def _get_file_checksum(self, file):
        """
        Calculate SHA256 checksum of uploaded file.
        """
        hash_sha256 = hashlib.sha256()
        for chunk in file.chunks(8192):
            hash_sha256.update(chunk)
        return hash_sha256.hexdigest()

    def _get_organization(self):
        """
        Get the organization for the authenticated user.
        """
        user = self.request.user
        if not user.organization:
            raise ValueError("User is not associated with any organization.")
        return user.organization

    def _get_candidate(self, organization, candidate_id=None, candidate_email=None):
        """
        Find candidate by ID or email within the organization.
        """
        try:
            if candidate_id:
                return Candidate.objects.get(id=candidate_id, organization=organization)
            elif candidate_email:
                return Candidate.objects.get(email=candidate_email, organization=organization)
        except Candidate.DoesNotExist:
            raise ValueError(f"Candidate not found in organization {organization.id}.")

    def _extract_candidate_from_cv(self, file_path, organization):
        """
        Extract candidate information from CV file.
        Returns a tuple of (first_name, last_name, email, phone, entities).
        """
        try:
            # Extract text from CV
            extraction = CVTextExtractor(timeout_seconds=30, save_to_file=False).extract_text(file_path)
            
            if not extraction.get('success') or not extraction.get('text'):
                raise ValueError("Failed to extract text from CV")
            
            # Extract entities
            entity_extractor = CVEntityExtractor()
            entities = entity_extractor.extract_entities(extraction.get('text'))
            
            # Get email (required)
            email = entities.email[0] if entities.email else None
            if not email:
                raise ValueError("No email found in CV")
            
            # Get phone
            phone = entities.phone[0] if entities.phone else None
            
            # Try to extract name from text (simple heuristic)
            # This is a basic implementation - you might want to improve this
            text_lines = extraction.get('text').split('\n')
            first_name = "Unknown"
            last_name = "Candidate"
            
            # Look for name in first few lines
            for line in text_lines[:5]:
                line = line.strip()
                # Skip empty lines and lines with only special characters
                if not line or len(line.split()) > 4:
                    continue
                # Assume name is 2-4 words
                words = line.split()
                if 2 <= len(words) <= 4 and not any(char in line for char in ['@', 'http', '+']):
                    first_name = words[0]
                    last_name = ' '.join(words[1:]) if len(words) > 1 else words[0]
                    break
            
            return first_name, last_name, email, phone, entities
            
        except Exception as e:
            raise ValueError(f"Failed to extract candidate info: {str(e)}")

    def _save_uploaded_file(self, file, organization, candidate):
        """
        Save uploaded file to disk.
        Returns the storage path relative to uploads directory.
        """
        uploads_dir = Path('/app/uploads') / str(organization.id) / str(candidate.id)
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # Use original filename
        file_path = uploads_dir / file.name
        with open(file_path, 'wb+') as destination:
            for chunk in file.chunks():
                destination.write(chunk)

        # Return relative path for storage in DB
        return str(file_path.relative_to('/app/uploads'))

    @transaction.atomic
    def post(self, request):
        """
        Handle CV file upload with validation and idempotent behavior.
        Supports automatic candidate creation from CV if auto_create_candidate=true.
        """
        try:
            # Get organization from authenticated user
            organization = self._get_organization()
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)

        # Extract candidate lookup parameters
        candidate_id = request.data.get('candidate_id')
        candidate_email = request.data.get('candidate_email')
        auto_create = request.data.get('auto_create_candidate', 'false')
        auto_create_flag = auto_create in ('true', 'True', '1', True)

        # Get or create candidate
        candidate = None
        candidate_created = False
        
        if candidate_id or candidate_email:
            # Try to find existing candidate
            try:
                candidate = self._get_candidate(organization, candidate_id, candidate_email)
            except ValueError as e:
                if not auto_create_flag:
                    return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)
        
        # If no candidate found/specified and auto_create is enabled
        if not candidate and auto_create_flag:
            # Validate serializer first to get the file
            serializer = CVUploadSerializer(data=request.data)
            if not serializer.is_valid():
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"CV Upload validation failed: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
            file = serializer.validated_data['file']
            
            # Save file temporarily to extract data
            temp_dir = Path('/app/uploads/temp')
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / file.name
            
            with open(temp_path, 'wb+') as destination:
                for chunk in file.chunks():
                    destination.write(chunk)
            
            try:
                # Extract candidate info from CV
                first_name, last_name, email, phone, entities = self._extract_candidate_from_cv(
                    str(temp_path), organization
                )
                
                # Check if candidate with this email already exists
                candidate = Candidate.objects.filter(
                    organization=organization,
                    email=email
                ).first()
                
                if not candidate:
                    # Create new candidate
                    candidate = Candidate.objects.create(
                        organization=organization,
                        first_name=first_name,
                        last_name=last_name,
                        email=email,
                        phone=phone,
                        status='new',
                    )
                    candidate_created = True
                
                # Reset file pointer for later use
                file.seek(0)
                
            finally:
                # Clean up temp file
                if temp_path.exists():
                    temp_path.unlink()
        
        if not candidate:
            return Response(
                {'error': 'No candidate specified. Provide candidate_id, candidate_email, or set auto_create_candidate=true'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate serializer
        serializer = CVUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Get uploaded file
        file = serializer.validated_data['file']

        # Calculate checksum
        checksum = self._get_file_checksum(file)

        # Check for duplicate (same checksum)
        existing_cv = CVFile.objects.filter(
            organization=organization,
            candidate=candidate,
            checksum=checksum,
        ).first()

        if existing_cv:
            # Idempotent: return existing record with 200 OK
            response_serializer = CVUploadSerializer(existing_cv)
            return Response(
                {
                    **response_serializer.data,
                    'message': 'File already uploaded with this checksum.',
                },
                status=status.HTTP_200_OK,
            )

        # Save file to disk
        try:
            storage_path = self._save_uploaded_file(file, organization, candidate)
        except IOError as e:
            return Response(
                {'error': f'Failed to save file: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Create CVFile record in database
        cv_file = CVFile.objects.create(
            organization=organization,
            candidate=candidate,
            storage_path=storage_path,
            original_filename=file.name,
            mime_type=file.content_type,
            file_size_bytes=file.size,
            checksum=checksum,
            upload_status='uploaded',
            source_type='upload',
        )

        # Log CV upload event
        CVAccessEventService.log_cv_upload(
            organization=organization,
            candidate=candidate,
            cv_file=cv_file,
            actor_user=request.user,
            metadata={
                'file_size': file.size,
                'mime_type': file.content_type,
            },
        )

        # Return response
        response_serializer = CVUploadSerializer(cv_file)

        timeout_seconds = request.data.get('timeout_seconds')
        save_to_file = request.data.get('save_to_file')
        try:
            timeout_seconds = int(timeout_seconds) if timeout_seconds else 30
        except (TypeError, ValueError):
            timeout_seconds = 30

        save_to_file_flag = True
        if isinstance(save_to_file, str):
            save_to_file_flag = save_to_file.lower() in ('true', '1', 'yes', 'on')
        elif isinstance(save_to_file, bool):
            save_to_file_flag = save_to_file

        extraction = CVTextExtractor(
            timeout_seconds=timeout_seconds,
            save_to_file=save_to_file_flag,
        ).extract_text(str(Path('/app/uploads') / storage_path))

        parse_status = (
            CVParseStatus.SUCCEEDED if extraction.get('success') else CVParseStatus.FAILED
        )
        cv_parse = CVParse.objects.create(
            organization=organization,
            cv_file=cv_file,
            parse_status=parse_status,
            parser_name=extraction.get('method'),
            parser_version='',
            parsed_at=timezone.now(),
            text_content=extraction.get('text', ''),
            error_message=extraction.get('error'),
        )

        # Extract entities if text extraction succeeded
        entities_data = None
        if extraction.get('success') and extraction.get('text'):
            try:
                entity_extractor = CVEntityExtractor()
                entities = entity_extractor.extract_entities(extraction.get('text'))
                
                # Save structured data
                CandidateStructuredData.objects.create(
                    organization=organization,
                    cv_parse=cv_parse,
                    structured_json=entities.to_dict(),
                    headline=' | '.join(entities.job_titles[:3]) if entities.job_titles else None,
                    primary_location=None,  # Could be extracted from contact info if available
                    top_skills=', '.join(
                        (entities.programming_languages or [])[:5] + 
                        (entities.frameworks or [])[:5]
                    )[:255] if entities.programming_languages or entities.frameworks else None,
                    experience_years=None,  # Could be calculated from experience dates
                )
                
                entities_data = entities.to_dict()
            except Exception as e:
                # Log error but don't fail the upload
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to extract entities: {str(e)}")
        
        # Generate embedding for semantic search
        if extraction.get('success') and extraction.get('text'):
            try:
                embedding_vector = EmbeddingService.generate_embedding(extraction.get('text'))
                if embedding_vector:
                    SearchDocument.objects.update_or_create(
                        organization=organization,
                        entity_type=SearchEntityType.CANDIDATE,
                        entity_id=candidate.id,
                        defaults={
                            'source_text': extraction.get('text')[:5000],  # Store first 5000 chars
                            'embedding': embedding_vector,
                            'index_status': 'indexed',
                            'indexed_at': timezone.now(),
                        }
                    )
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to generate embedding: {str(e)}")

        return Response(
            {
                **response_serializer.data,
                'candidate_created': candidate_created,
                'candidate_id': str(candidate.id),
                'candidate_name': f"{candidate.first_name} {candidate.last_name}",
                'success': extraction.get('success', False),
                'extracted_text': extraction.get('text', ''),
                'metadata': extraction.get('metadata', {}),
                'method': extraction.get('method'),
                'output_file': extraction.get('output_file'),
                'error': extraction.get('error'),
                'entities': entities_data,
            },
            status=status.HTTP_201_CREATED,
        )


class AuditLogListView(APIView):
    """
    API endpoint to query audit logs for the organization.
    Supports filtering by event type, entity type, severity, etc.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Get audit logs with optional filters.

        Query parameters:
        - event_type: Filter by event type
        - entity_type: Filter by entity type
        - severity: Filter by severity (log, debug, verbose)
        - limit: Number of results (default 100, max 1000)
        """
        org = request.user.organization
        if not org:
            return Response(
                {"detail": "User has no organization"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Get query parameters
        event_type = request.query_params.get('event_type')
        entity_type = request.query_params.get('entity_type')
        severity = request.query_params.get('severity')
        limit = min(int(request.query_params.get('limit', 100)), 1000)

        # Query logs
        logs = AuditLogService.query_logs(
            organization=org,
            event_type=event_type,
            entity_type=entity_type,
            severity=severity,
            limit=limit,
        )

        # Serialize and return
        serializer = AuditLogSerializer(logs, many=True)
        return Response({
            'count': len(serializer.data),
            'results': serializer.data,
        })


class UserMeView(APIView):
    """
    API endpoint to get/update the current user.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'user': RegisterSerializer(request.user).data})

    def patch(self, request):
        serializer = UserUpdateSerializer(
            data=request.data, context={'user': request.user}
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.update(request.user, serializer.validated_data)
        return Response({'user': RegisterSerializer(user).data})


class CVAccessEventListView(APIView):
    """
    API endpoint to query CV access events for the organization.
    Supports filtering by candidate, action, etc.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Get CV access events with optional filters.

        Query parameters:
        - candidate_id: Filter by candidate UUID
        - action: Filter by action type
        - limit: Number of results (default 100, max 1000)
        """
        org = request.user.organization
        if not org:
            return Response(
                {"detail": "User has no organization"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Get query parameters
        candidate_id = request.query_params.get('candidate_id')
        action = request.query_params.get('action')
        limit = min(int(request.query_params.get('limit', 100)), 1000)

        # Get candidate if filtering
        candidate = None
        if candidate_id:
            try:
                candidate = Candidate.objects.get(id=candidate_id, organization=org)
            except Candidate.DoesNotExist:
                return Response(
                    {"detail": "Candidate not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

        # Query events
        events = CVAccessEventService.query_events(
            organization=org,
            candidate=candidate,
            action=action,
            limit=limit,
        )

        # Serialize and return
        serializer = CVAccessEventSerializer(events, many=True)
        return Response({
            'count': len(serializer.data),
            'results': serializer.data,
        })


class RankingEventListView(APIView):
    """
    API endpoint to query ranking-specific audit events.
    Provides detailed tracking of ranking runs and their outcomes.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Get ranking events with optional filters.

        Query parameters:
        - run_id: Filter by ranking run UUID
        - event_type: Filter by event type (started, completed, failed, etc.)
        - limit: Number of results (default 100, max 1000)
        """
        org = request.user.organization
        if not org:
            return Response(
                {"detail": "User has no organization"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Get query parameters
        run_id = request.query_params.get('run_id')
        event_type = request.query_params.get('event_type')
        severity = request.query_params.get('severity')
        limit = min(int(request.query_params.get('limit', 100)), 1000)

        # Build filter for audit events
        from accounts.models import AuditLog
        
        # Exclude low-level HTTP events - HR only needs business events
        queryset = AuditLog.objects.filter(organization=org).exclude(
            event_type__startswith='http.'
        )
        
        # Filter by run_id if provided
        if run_id:
            queryset = queryset.filter(entity_id=run_id)
        
        # Optionally filter by event type
        if event_type:
            queryset = queryset.filter(event_type=event_type)
        
        # Filter by severity if provided
        if severity:
            queryset = queryset.filter(severity__iexact=severity)

        # Order and limit
        queryset = queryset.order_by('-created_at')[:limit]

        # Serialize and return
        serializer = AuditLogSerializer(queryset, many=True)
        
        # Calculate statistics if filtering by run_id
        stats = None
        if run_id and serializer.data:
            stats = self._calculate_run_stats(serializer.data)

        return Response({
            'count': len(serializer.data),
            'results': serializer.data,
            'statistics': stats,
        })

    def _calculate_run_stats(self, events: list) -> dict:
        """Calculate statistics from ranking run events."""
        stats = {
            'total_events': len(events),
            'event_types': {},
            'has_completion': False,
            'has_failure': False,
        }

        for event in events:
            event_type = event.get('event_type', '')
            stats['event_types'][event_type] = stats['event_types'].get(event_type, 0) + 1

            if 'completed' in event_type:
                stats['has_completion'] = True
                # Extract performance metrics from metadata
                metadata = event.get('metadata', {})
                stats['candidates_evaluated'] = metadata.get('candidates_evaluated')
                stats['scores_created'] = metadata.get('scores_created')
                stats['total_duration_seconds'] = metadata.get('total_duration_seconds')
                stats['score_statistics'] = metadata.get('score_statistics')

            if 'failed' in event_type:
                stats['has_failure'] = True
                metadata = event.get('metadata', {})
                stats['error'] = metadata.get('error')
                stats['error_type'] = metadata.get('error_type')

        return stats


class OrganizationListCreateView(APIView):
    """
    API endpoint to list or create organizations.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        organizations = OrganizationSerializer(
            self._get_organizations(), many=True
        )
        return Response({'results': organizations.data})

    def post(self, request):
        if request.user.organization:
            return Response(
                {'detail': 'User already belongs to an organization.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = OrganizationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = serializer.save()

        request.user.organization = organization
        request.user.save(update_fields=['organization'])

        return Response(
            {'organization': OrganizationSerializer(organization).data},
            status=status.HTTP_201_CREATED,
        )

    def _get_organizations(self):
        return self._organization_queryset()

    def _organization_queryset(self):
        from accounts.models import Organization
        return Organization.objects.all().order_by('name')


class OrganizationSelectView(APIView):
    """
    API endpoint to assign the current user to an organization.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        serializer = OrganizationSelectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = serializer.validated_data['organization_id']
        request.user.organization = organization
        request.user.save(update_fields=['organization'])
        return Response({'organization': OrganizationSerializer(organization).data})


class CandidateListCreateView(APIView):
    """
    API endpoint to list or create candidates within the user's organization.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        organization = request.user.organization
        
        # Job seekers (individuals) don't have organizations, return empty list
        if not organization:
            if hasattr(request.user, 'user_type') and request.user.user_type == 'job_seeker':
                return Response({'results': []})
            return Response(
                {'detail': 'User has no organization.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        queryset = Candidate.objects.filter(organization=organization).order_by(
            'first_name', 'last_name'
        )
        serializer = CandidateBasicSerializer(queryset, many=True)
        return Response({'results': serializer.data})

    def post(self, request):
        organization = request.user.organization
        if not organization:
            return Response(
                {'detail': 'User has no organization.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CandidateCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Check if candidate already exists
        email = serializer.validated_data.get('email')
        existing_candidate = None
        if email:
            existing_candidate = Candidate.objects.filter(
                organization=organization,
                email=email
            ).first()
        
        if existing_candidate:
            # Return existing candidate with 200 OK (idempotent)
            return Response(
                {
                    'candidate': CandidateCreateSerializer(existing_candidate).data,
                    'message': 'Candidate with this email already exists.',
                },
                status=status.HTTP_200_OK,
            )
        
        # Create new candidate
        candidate = serializer.save(organization=organization)
        
        # Log candidate creation
        AuditLogService.log(
            organization=organization,
            event_type='candidate.created',
            entity_type='candidate',
            entity_id=candidate.id,
            actor_user=request.user,
            description=f"Created candidate {candidate.first_name} {candidate.last_name}",
            metadata={
                'email': candidate.email,
                'phone': candidate.phone,
            },
            severity=AuditSeverity.LOG,
        )
        
        return Response(
            {'candidate': CandidateCreateSerializer(candidate).data},
            status=status.HTTP_201_CREATED,
        )


class CVFileListDeleteView(APIView):
    """
    API endpoint to list uploaded CV files and delete a file.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        organization = request.user.organization
        if not organization:
            return Response(
                {'detail': 'User has no organization.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        queryset = CVFile.objects.filter(organization=organization).select_related(
            'candidate'
        )
        serializer = CVFileListSerializer(queryset, many=True)
        return Response({'results': serializer.data})

    def delete(self, request, cv_file_id):
        organization = request.user.organization
        if not organization:
            return Response(
                {'detail': 'User has no organization.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            cv_file = CVFile.objects.get(id=cv_file_id, organization=organization)
        except CVFile.DoesNotExist:
            return Response({'detail': 'CV file not found.'}, status=status.HTTP_404_NOT_FOUND)

        storage_path = Path('/app/uploads') / cv_file.storage_path
        extracted_path = storage_path.with_name(
            f"{storage_path.stem}_extracted.txt"
        )

        # Log CV file deletion
        AuditLogService.log(
            organization=organization,
            event_type='cv.deleted',
            entity_type='cv_file',
            entity_id=cv_file.id,
            actor_user=request.user,
            description=f"Deleted CV file {cv_file.original_filename} for {cv_file.candidate.first_name} {cv_file.candidate.last_name}",
            metadata={
                'filename': cv_file.original_filename,
                'candidate_id': str(cv_file.candidate.id),
                'candidate_name': f"{cv_file.candidate.first_name} {cv_file.candidate.last_name}",
            },
            severity=AuditSeverity.LOG,
        )
        
        cv_file.cv_parses.all().delete()
        cv_file.delete()

        try:
            if storage_path.exists():
                storage_path.unlink()
            if extracted_path.exists():
                extracted_path.unlink()
        except OSError:
            pass

        return Response(status=status.HTTP_204_NO_CONTENT)



from django.db.models import OuterRef, Subquery, Q
from rest_framework.permissions import IsAuthenticated

from .models import (
    CandidateStructuredData,
    CVParse,
    CandidateSummary,
    SummaryStatus,
    CVAccessAction,
)
from .serializers import (
    NLQParseRequestSerializer,
    NLQParseResponseSerializer,
    CandidateSearchRequestSerializer,
    CandidateSearchResultSerializer,
    CandidateSummaryRequestSerializer,
    CandidateSummaryResponseSerializer,
)
from .ai import parse_nlq, generate_summary, MakeAIError


def _latest_structured_subquery(field_name: str):
    latest_struct = CandidateStructuredData.objects.filter(
        cv_parse__cv_file__candidate=OuterRef("pk")
    ).order_by("-created_at")
    return Subquery(latest_struct.values(field_name)[:1])

def _count_structured_items(structured_json: dict | None, keys: list[str]) -> int:
    if not isinstance(structured_json, dict):
        return 0
    total = 0
    for key in keys:
        items = structured_json.get(key) or []
        if isinstance(items, list):
            total += len([item for item in items if str(item).strip()])
        elif isinstance(items, str):
            if items.strip():
                total += 1
    return total


def _compute_simple_score(
    must_have: list[str],
    nice_to_have: list[str],
    keywords: list[str],
    top_skills_text: str,
    headline: str,
    experience_years: float | None,
    min_years: float | None,
) -> tuple[float, str]:
    text = f"{top_skills_text or ''} {headline or ''}".lower()

    must = [s.strip().lower() for s in must_have if s.strip()]
    nice = [s.strip().lower() for s in nice_to_have if s.strip()]
    keys = [k.strip().lower() for k in keywords if k.strip()]

    must_hits = sum(1 for s in must if s in text)
    nice_hits = sum(1 for s in nice if s in text)
    key_hits = sum(1 for k in keys if k in text)

    must_ratio = (must_hits / max(len(must), 1)) if must else 1.0

    years_ok = True
    if min_years is not None:
        if experience_years is None:
            years_ok = False
        else:
            years_ok = float(experience_years) >= float(min_years)

    score = 0.0
    score += 0.60 * must_ratio
    score += 0.20 * min(nice_hits, 5) / 5.0
    score += 0.20 * min(key_hits, 5) / 5.0
    if not years_ok:
        score *= 0.4

    expl = (
        f"must_have_hits={must_hits}/{len(must)}; "
        f"nice_hits={nice_hits}; keyword_hits={key_hits}; "
        f"years_ok={years_ok}"
    )
    return round(score, 2), expl


class NLQParseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        s = NLQParseRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        query = s.validated_data["query"]
        language = s.validated_data.get("language", "hu")

        # Try external AI service first (Make.com webhook)
        try:
            data = parse_nlq(query=query, language=language)
        except MakeAIError as e:
            # Fallback to local NLQ parser
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Make.com webhook unavailable, using local NLQ parser: {e}")
            
            try:
                data = LocalNLQParser.parse(query=query, language=language)
            except Exception as parse_error:
                return Response(
                    {"detail": f"NLQ parsing failed: {str(parse_error)}"}, 
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        out = NLQParseResponseSerializer(data=data)
        if not out.is_valid():
            return Response(
                {"detail": "Invalid NLQ payload from Make", "errors": out.errors},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # audit: search action (opcionális, de jó)
        org = request.user.organization
        if org:
            CVAccessEventService.log_event(
                organization=org,
                action=CVAccessAction.SEARCHED,
                candidate=Candidate.objects.filter(organization=org).first(),  # hack: event schema candidate-kötött
                actor_user=request.user,
                channel="api",
                metadata={"nlq": True},
            )

        return Response(out.validated_data, status=status.HTTP_200_OK)


class CandidateSearchView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        s = CandidateSearchRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._search(request, s.validated_data)

    def get(self, request):
        payload = {
            "must_have_skills": (request.query_params.get("must_have", "")).split(",") if request.query_params.get("must_have") else [],
            "nice_to_have_skills": (request.query_params.get("nice_to_have", "")).split(",") if request.query_params.get("nice_to_have") else [],
            "min_years_experience": request.query_params.get("min_years_experience"),
            "location": request.query_params.get("location", ""),
            "remote": request.query_params.get("remote"),
            "keywords": (request.query_params.get("keywords", "")).split(",") if request.query_params.get("keywords") else [],
            "sort": request.query_params.get("sort", "score_desc"),
        }

        if payload["min_years_experience"] not in (None, ""):
            payload["min_years_experience"] = float(payload["min_years_experience"])
        else:
            payload["min_years_experience"] = None

        if payload["remote"] in ("true", "1", "yes"):
            payload["remote"] = True
        elif payload["remote"] in ("false", "0", "no"):
            payload["remote"] = False
        else:
            payload["remote"] = None

        s = CandidateSearchRequestSerializer(data=payload)
        s.is_valid(raise_exception=True)
        return self._search(request, s.validated_data)

    def _search(self, request, filters: dict):
        org = request.user.organization
        if not org:
            return Response({"detail": "User has no organization."}, status=status.HTTP_403_FORBIDDEN)

        must_have = filters.get("must_have_skills", [])
        nice_to_have = filters.get("nice_to_have_skills", [])
        min_years = filters.get("min_years_experience")
        location = (filters.get("location") or "").strip()
        keywords = filters.get("keywords", [])
        sort = filters.get("sort", "score_desc")
        
        # If we have keywords, try vector search first
        vector_scores = {}
        if keywords:
            query_text = " ".join([k.strip() for k in keywords if k.strip()])
            if query_text:
                try:
                    query_embedding = EmbeddingService.generate_embedding(query_text)
                    if query_embedding:
                        # Use pgvector for semantic search
                        from django.db.models import F
                        from pgvector.django import CosineDistance
                        
                        search_docs = SearchDocument.objects.filter(
                            organization=org,
                            entity_type=SearchEntityType.CANDIDATE,
                            embedding__isnull=False
                        ).annotate(
                            distance=CosineDistance('embedding', query_embedding)
                        ).order_by('distance')[:50]
                        
                        # Convert distance to similarity score (0-1)
                        for doc in search_docs:
                            similarity = 1.0 - doc.distance
                            vector_scores[str(doc.entity_id)] = max(0.0, similarity)
                except Exception as e:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.warning(f"Vector search failed, falling back to text search: {e}")

        qs = Candidate.objects.filter(organization=org)

        qs = qs.annotate(
            latest_headline=_latest_structured_subquery("headline"),
            latest_primary_location=_latest_structured_subquery("primary_location"),
            latest_experience_years=_latest_structured_subquery("experience_years"),
            latest_top_skills=_latest_structured_subquery("top_skills"),
            latest_structured_json=_latest_structured_subquery("structured_json"),
        )

        if location:
            qs = qs.filter(Q(latest_primary_location__icontains=location))

        if min_years is not None:
            qs = qs.filter(latest_experience_years__gte=min_years)

        # Only apply strict skill filtering if no vector search is performed
        if not vector_scores:
            for skill in [s.strip() for s in must_have if s.strip()]:
                qs = qs.filter(
                    Q(latest_top_skills__icontains=skill) | Q(latest_headline__icontains=skill)
                )

        if keywords:
            kw_q = Q()
            for k in [x.strip() for x in keywords if x.strip()]:
                kw_q |= Q(latest_top_skills__icontains=k) | Q(latest_headline__icontains=k)
            qs = qs.filter(kw_q)

        qs = qs[:200]

        results = []
        skill_keys = [
            "programming_languages",
            "frameworks",
            "databases",
            "tools",
            "cloud_platforms",
            "soft_skills",
        ]
        education_keys = ["degrees", "certifications"]
        for c in qs:
            exp = getattr(c, "latest_experience_years", None)
            exp_f = float(exp) if exp is not None else None
            structured_json = getattr(c, "latest_structured_json", None)
            skill_count = _count_structured_items(structured_json, skill_keys)
            education_count = _count_structured_items(structured_json, education_keys)
            language_count = _count_structured_items(structured_json, ["languages"])

            score, expl = _compute_simple_score(
                must_have=must_have,
                nice_to_have=nice_to_have,
                keywords=keywords,
                top_skills_text=getattr(c, "latest_top_skills", "") or "",
                headline=getattr(c, "latest_headline", "") or "",
                experience_years=exp_f,
                min_years=min_years,
            )
            
            # Combine with vector score if available
            candidate_id_str = str(c.id)
            if candidate_id_str in vector_scores:
                vector_score = vector_scores[candidate_id_str]
                # Weighted combination: 60% text-based, 40% vector-based
                combined_score = (0.6 * score) + (0.4 * vector_score)
                expl += f" | Vector similarity: {vector_score:.2f}"
                score = combined_score

            results.append({
                "id": c.id,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "email": c.email,
                "status": c.status,
                "headline": getattr(c, "latest_headline", "") or "",
                "primary_location": getattr(c, "latest_primary_location", "") or "",
                "experience_years": exp_f,
                "top_skills": getattr(c, "latest_top_skills", "") or "",
                "education_count": education_count,
                "language_count": language_count,
                "skill_count": skill_count,
                "score": score,
                "score_explanation": expl,
            })

        if sort == "experience_desc":
            results.sort(
                key=lambda x: (
                    x["experience_years"] is None,
                    -(x["experience_years"] or 0),
                    -x["score"],
                    x["last_name"].lower(),
                    x["first_name"].lower(),
                )
            )
        elif sort == "education_desc":
            results.sort(
                key=lambda x: (
                    -x["education_count"],
                    -x["score"],
                    x["last_name"].lower(),
                    x["first_name"].lower(),
                )
            )
        elif sort == "language_desc":
            results.sort(
                key=lambda x: (
                    -x["language_count"],
                    -x["score"],
                    x["last_name"].lower(),
                    x["first_name"].lower(),
                )
            )
        elif sort == "skills_desc":
            results.sort(
                key=lambda x: (
                    -x["skill_count"],
                    -x["score"],
                    x["last_name"].lower(),
                    x["first_name"].lower(),
                )
            )
        elif sort == "name_asc":
            results.sort(key=lambda x: (x["last_name"].lower(), x["first_name"].lower()))
        else:
            results.sort(
                key=lambda x: (
                    -x["score"],
                    x["last_name"].lower(),
                    x["first_name"].lower(),
                )
            )

        out = CandidateSearchResultSerializer(results, many=True)
        return Response({"count": len(results), "results": out.data})


class CandidateSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, candidate_id):
        org = request.user.organization
        if not org:
            return Response({"detail": "User has no organization."}, status=status.HTTP_403_FORBIDDEN)

        try:
            candidate = Candidate.objects.get(id=candidate_id, organization=org)
        except Candidate.DoesNotExist:
            return Response({"detail": "Candidate not found."}, status=status.HTTP_404_NOT_FOUND)

        s = CandidateSummaryRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        language = s.validated_data.get("language", "hu")
        job_text = s.validated_data.get("job_text")
        # New parameter to choose summary method: "template" (default) or "ai"
        method = request.data.get("method", "template")

        latest_parse = CVParse.objects.filter(
            organization=org,
            cv_file__candidate=candidate,
            text_content__isnull=False,
        ).exclude(text_content="").order_by("-created_at").first()

        if not latest_parse:
            return Response({"detail": "No parsed CV text available for this candidate."}, status=status.HTTP_400_BAD_REQUEST)

        # Check for cached summary with the same method
        cached = CandidateSummary.objects.filter(
            organization=org,
            candidate=candidate,
            cv_parse=latest_parse,
            language=language,
            summary_status=SummaryStatus.SUCCEEDED,
            model_name=method,  # Cache per method
        ).order_by("-created_at").first()

        if cached and cached.summary_text:
            return Response({
                "summary": cached.summary_text,
                "highlights": [],
                "risks": [],
                "fit_score_explanation": "",
                "method": method,
            })

        # Create summary record
        row = CandidateSummary.objects.create(
            organization=org,
            candidate=candidate,
            cv_parse=latest_parse,
            language=language,
            summary_status=SummaryStatus.PENDING,
            prompt_version="v1.0",
            model_name=method,
        )

        try:
            if method == "ai":
                # Use existing AI-based summary (Make.com webhook)
                ai = generate_summary(
                    cv_text=latest_parse.text_content or "",
                    language=language,
                    job_text=job_text,
                )

                resp = CandidateSummaryResponseSerializer(data=ai)
                resp.is_valid(raise_exception=True)

                row.summary_status = SummaryStatus.SUCCEEDED
                row.summary_text = resp.validated_data["summary"]
                row.generated_at = timezone.now()
                row.model_version = ai.get("model_version", "")
                row.save(update_fields=[
                    "summary_status",
                    "summary_text",
                    "generated_at",
                    "model_version",
                    "updated_at",
                ])

                result = resp.validated_data
            else:
                # Use template-based summary (no hallucinations, uses only extracted entities)
                # Get structured data (extracted entities)
                structured_data = CandidateStructuredData.objects.filter(
                    cv_parse=latest_parse,
                    organization=org
                ).first()

                if not structured_data or not structured_data.structured_json:
                    return Response(
                        {"detail": "No structured data available for this candidate. Please ensure CV has been processed."},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # Convert structured_json to ExtractedEntities
                entities = ExtractedEntities.from_dict(structured_data.structured_json)
                
                # Generate template-based summary
                summarizer = CVSummarizer(
                    language=language,
                    model_name="template",
                    model_version="1.0",
                    prompt_version="1.0"
                )
                
                cv_summary = summarizer.generate_summary(entities, language=language)

                # Update database record
                row.summary_status = SummaryStatus.SUCCEEDED
                row.summary_text = cv_summary.summary_text
                row.generated_at = cv_summary.generated_at
                row.model_version = cv_summary.model_version
                row.prompt_version = cv_summary.prompt_version
                row.save(update_fields=[
                    "summary_status",
                    "summary_text",
                    "generated_at",
                    "model_version",
                    "prompt_version",
                    "updated_at",
                ])

                result = {
                    "summary": cv_summary.summary_text,
                    "highlights": [],
                    "risks": [],
                    "fit_score_explanation": "",
                    "method": "template",
                }

            CVAccessEventService.log_event(
                organization=org,
                action=CVAccessAction.SUMMARY_GENERATED,
                candidate=candidate,
                actor_user=request.user,
                cv_file=latest_parse.cv_file,
                cv_parse=latest_parse,
                channel="api",
                metadata={"language": language, "method": method, "prompt_version": "v1.0"},
            )

            return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
            row.summary_status = SummaryStatus.FAILED
            row.error_message = str(e)
            row.save(update_fields=["summary_status", "error_message", "updated_at"])
            return Response({"detail": f"Summary generation failed: {e}"}, status=status.HTTP_502_BAD_GATEWAY)


class CandidateStructuredDataView(APIView):
    """
    API endpoint to get structured data for a candidate.
    Returns the latest extracted entities from their CV.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, candidate_id):
        org = request.user.organization
        if not org:
            return Response(
                {"detail": "User has no organization"},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            candidate = Candidate.objects.get(
                id=candidate_id,
                organization=org
            )
        except Candidate.DoesNotExist:
            return Response(
                {"detail": "Candidate not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Get latest structured data
        latest_data = CandidateStructuredData.objects.filter(
            cv_parse__cv_file__candidate=candidate,
            organization=org
        ).select_related(
            'cv_parse__cv_file__candidate'
        ).order_by('-created_at').first()

        if not latest_data:
            return Response(
                {"detail": "No structured data available for this candidate"},
                status=status.HTTP_404_NOT_FOUND
            )

        from .serializers import CandidateStructuredDataSerializer
        serializer = CandidateStructuredDataSerializer(latest_data)
        return Response(serializer.data, status=status.HTTP_200_OK)


class CandidateDetailView(APIView):
    """
    Retrieve, update or delete a candidate instance.
    """
    permission_classes = [IsAuthenticated]

    def get_object(self, pk):
        try:
            return Candidate.objects.get(pk=pk)
        except Candidate.DoesNotExist:
            raise Http404

    def delete(self, request, pk, format=None):
        candidate = self.get_object(pk)
        # Check if the user belongs to the same organization as the candidate
        if candidate.organization != request.user.organization:
            return Response(status=status.HTTP_403_FORBIDDEN)
        
        # Log candidate deletion
        AuditLogService.log(
            organization=candidate.organization,
            event_type='candidate.deleted',
            entity_type='candidate',
            entity_id=candidate.id,
            actor_user=request.user,
            description=f"Deleted candidate {candidate.first_name} {candidate.last_name}",
            metadata={
                'email': candidate.email,
            },
            severity=AuditSeverity.LOG,
        )
        
        candidate.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

