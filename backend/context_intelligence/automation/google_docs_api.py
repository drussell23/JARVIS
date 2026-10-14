"""
Google Docs API Integration
============================

Provides Google Docs API functionality for document creation and editing.
Uses OAuth 2.0 for authentication and Google Docs/Drive APIs for operations.
"""

import asyncio
import logging
import os
import json
from typing import Optional, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

# Try to import Google API libraries
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False
    from backend.core.optional_dependencies import note_missing
    note_missing("google-api-python-client", "Google Docs", install="google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")

# OAuth 2.0 scopes for Google Docs
SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive.file'
]


class GoogleDocsClient:
    """Client for Google Docs API operations"""

    def __init__(self, credentials_path: Optional[str] = None, token_path: Optional[str] = None):
        """Initialize Google Docs client"""
        self.credentials_path = credentials_path or os.getenv(
            'GOOGLE_CREDENTIALS_PATH',
            str(Path.home() / '.jarvis' / 'google_credentials.json')
        )
        self.token_path = token_path or os.getenv(
            'GOOGLE_TOKEN_PATH',
            str(Path.home() / '.jarvis' / 'google_token.json')
        )
        self._creds = None
        self._docs_service = None
        self._drive_service = None

    async def authenticate(self) -> bool:
        """Authenticate with Google API"""
        if not GOOGLE_API_AVAILABLE:
            logger.error("Google API libraries not available")
            return False

        try:
            # Check if token exists
            if os.path.exists(self.token_path):
                self._creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

            # If no valid credentials, authenticate
            if not self._creds or not self._creds.valid:
                if self._creds and self._creds.expired and self._creds.refresh_token:
                    logger.info("Refreshing Google OAuth token...")
                    self._creds.refresh(Request())
                else:
                    if not os.path.exists(self.credentials_path):
                        logger.error(f"Google credentials file not found: {self.credentials_path}")
                        return False

                    logger.info("Starting OAuth flow...")
                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
                    self._creds = flow.run_local_server(port=0)

                # Save credentials
                os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
                with open(self.token_path, 'w') as token:
                    token.write(self._creds.to_json())

            # Build services
            self._docs_service = build('docs', 'v1', credentials=self._creds)
            self._drive_service = build('drive', 'v3', credentials=self._creds)

            logger.info("✅ Google Docs API authenticated")
            return True

        except Exception as e:
            logger.error(f"Failed to authenticate: {e}")
            return False

    async def create_document(self, title: str) -> Optional[Dict[str, Any]]:
        """Create a new Google Doc"""
        if not self._docs_service:
            if not await self.authenticate():
                return None

        try:
            doc = self._docs_service.documents().create(body={'title': title}).execute()
            document_id = doc.get('documentId')
            document_url = f"https://docs.google.com/document/d/{document_id}/edit"

            logger.info(f"Created Google Doc: {title} ({document_id})")
            return {
                'document_id': document_id,
                'document_url': document_url,
                'title': title
            }

        except HttpError as e:
            logger.error(f"Error creating document: {e}")
            return None

    async def append_text(self, document_id: str, text: str) -> bool:
        """Append text to end of document"""
        if not self._docs_service:
            if not await self.authenticate():
                return False

        try:
            # Get current document to find end index
            doc = self._docs_service.documents().get(documentId=document_id).execute()
            content = doc.get('body').get('content')
            end_index = content[-1].get('endIndex', 1) - 1

            # Insert text at end
            requests = [{
                'insertText': {
                    'location': {'index': end_index},
                    'text': text
                }
            }]

            self._docs_service.documents().batchUpdate(
                documentId=document_id,
                body={'requests': requests}
            ).execute()

            return True

        except HttpError as e:
            logger.error(f"Error appending text: {e}")
            return False


# Global instance
_google_docs_client: Optional[GoogleDocsClient] = None


def get_google_docs_client(credentials_path: Optional[str] = None,
                           token_path: Optional[str] = None) -> GoogleDocsClient:
    """Get or create global Google Docs client instance"""
    global _google_docs_client
    if _google_docs_client is None:
        _google_docs_client = GoogleDocsClient(credentials_path, token_path)
    return _google_docs_client
