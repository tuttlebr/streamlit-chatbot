"""
PDF Summarization Service

This service handles recursive summarization of large PDF documents
to avoid timeout issues by processing pages in batches.
"""

import asyncio
import concurrent.futures
import logging
from typing import Dict, List, Optional

from models.chat_config import ChatConfig
from utils.batch_processor import BatchProcessor, DocumentProcessor
from utils.config import config
from utils.streamlit_context import run_with_streamlit_context

logger = logging.getLogger(__name__)


class PDFSummarizationService:
    """Service for handling recursive PDF summarization"""

    def __init__(self, config_obj: ChatConfig):
        """
        Initialize the PDF summarization service

        Args:
            config_obj: Configuration for the service
        """
        self.config = config_obj
        self.batch_size = config.file_processing.PDF_SUMMARIZATION_BATCH_SIZE
        self.max_summary_length = config.file_processing.PDF_SUMMARY_MAX_LENGTH
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)

    async def summarize_pdf_recursive(self, pdf_data: Dict) -> Dict:
        """
        Perform recursive summarization on PDF data

        Args:
            pdf_data: PDF data from NVINGEST containing pages

        Returns:
            Dictionary containing original data plus summaries
        """
        try:
            pages = pdf_data.get('pages', [])
            total_pages = len(pages)
            filename = pdf_data.get('filename', 'Unknown')

            logger.info(f"Starting recursive summarization for {filename} ({total_pages} pages)")

            if total_pages == 0:
                return pdf_data

            # Phase 1: Summarize individual pages or small batches
            page_summaries = await self._summarize_pages_in_batches(pages, filename)

            # Phase 2: Create intermediate summaries if needed (for very large documents)
            if len(page_summaries) > 10:
                intermediate_summaries = await self._create_intermediate_summaries(page_summaries)
            else:
                intermediate_summaries = page_summaries

            # Phase 3: Create final document summary
            final_summary = await self._create_final_summary(intermediate_summaries, filename)

            # Add summaries to the PDF data
            enhanced_pdf_data = pdf_data.copy()
            enhanced_pdf_data['page_summaries'] = page_summaries
            enhanced_pdf_data['document_summary'] = final_summary
            enhanced_pdf_data['summarization_complete'] = True

            logger.info(f"Completed recursive summarization for {filename}")
            return enhanced_pdf_data

        except Exception as e:
            logger.error(f"Error in recursive summarization: {e}")
            # Return original data if summarization fails
            return pdf_data

    async def _summarize_pages_in_batches(self, pages: List[Dict], filename: str) -> List[Dict]:
        """
        Summarize pages in batches to avoid memory issues

        Args:
            pages: List of page data
            filename: Name of the PDF file

        Returns:
            List of page summaries
        """
        batch_processor = BatchProcessor(batch_size=self.batch_size, delay_between_batches=0.5)

        async def summarize_batch(batch_pages: List[Dict], start_idx: int, end_idx: int) -> Dict:
            """Summarize a single batch of pages"""
            logger.info(f"Processing pages {start_idx+1}-{end_idx} of {len(pages)} for {filename}")

            # Use DocumentProcessor to format pages
            batch_text = DocumentProcessor.format_pages_for_analysis(batch_pages)

            try:
                summary_params = {
                    "task_type": "summarize",
                    "text": batch_text,
                    "instructions": f"Create a concise summary of these {len(batch_pages)} pages from a PDF document. Focus on key information, main topics, and important details. Maximum {self.max_summary_length} words.",
                }

                # Import locally to avoid circular imports
                from tools.assistant import execute_assistant_with_dict

                loop = asyncio.get_event_loop()
                summary_result = await loop.run_in_executor(
                    self.executor, run_with_streamlit_context, execute_assistant_with_dict, summary_params
                )

                return {
                    "page_range": f"{start_idx+1}-{end_idx}",
                    "summary": summary_result.result,
                    "pages_covered": end_idx - start_idx,
                }

            except Exception as e:
                logger.error(f"Error summarizing pages {start_idx+1}-{end_idx}: {e}")
                return {
                    "page_range": f"{start_idx+1}-{end_idx}",
                    "summary": "Summary unavailable due to processing error",
                    "pages_covered": end_idx - start_idx,
                }

        # Process all pages and filter out None results
        summaries = await batch_processor.process_in_batches(pages, summarize_batch)
        return [s for s in summaries if s is not None]

    async def _create_intermediate_summaries(self, page_summaries: List[Dict]) -> List[Dict]:
        """
        Create intermediate summaries for very large documents

        Args:
            page_summaries: List of page-level summaries

        Returns:
            List of intermediate summaries
        """
        intermediate_summaries = []
        batch_size = 5  # Combine 5 page summaries at a time

        for i in range(0, len(page_summaries), batch_size):
            batch_end = min(i + batch_size, len(page_summaries))
            batch_summaries = page_summaries[i:batch_end]

            # Combine summaries
            combined_text = "\n\n".join([f"Section {s['page_range']}:\n{s['summary']}" for s in batch_summaries])

            try:
                summary_params = {
                    "task_type": "summarize",
                    "text": combined_text,
                    "instructions": "Create a cohesive summary that combines these section summaries. Maintain key information while reducing redundancy.",
                }

                # Import locally to avoid circular imports
                from tools.assistant import execute_assistant_with_dict

                loop = asyncio.get_event_loop()
                summary_result = await loop.run_in_executor(
                    self.executor, run_with_streamlit_context, execute_assistant_with_dict, summary_params
                )

                intermediate_summaries.append(
                    {"sections_covered": [s['page_range'] for s in batch_summaries], "summary": summary_result.result}
                )

            except Exception as e:
                logger.error(f"Error creating intermediate summary: {e}")
                # Fallback to original summaries if intermediate fails
                intermediate_summaries.extend(batch_summaries)

        return intermediate_summaries

    async def _create_final_summary(self, summaries: List[Dict], filename: str) -> str:
        """
        Create the final document summary

        Args:
            summaries: List of intermediate or page summaries
            filename: Name of the PDF file

        Returns:
            Final document summary
        """
        try:
            # Combine all summaries
            if len(summaries) == 1:
                # If only one summary, use it directly
                return summaries[0].get('summary', '')

            combined_text = "\n\n".join([s.get('summary', '') for s in summaries])

            summary_params = {
                "task_type": "summarize",
                "text": combined_text,
                "instructions": f"Create a comprehensive executive summary of the entire document '{filename}'. Include main topics, key findings, important details, and overall conclusions. Make it informative yet concise.",
            }

            # Import locally to avoid circular imports
            from tools.assistant import execute_assistant_with_dict

            loop = asyncio.get_event_loop()
            summary_result = await loop.run_in_executor(
                self.executor, run_with_streamlit_context, execute_assistant_with_dict, summary_params
            )

            return summary_result.result

        except Exception as e:
            logger.error(f"Error creating final summary: {e}")
            return "Document summary unavailable due to processing error"

    def get_summary_for_context(self, pdf_data: Dict) -> Optional[str]:
        """
        Get a formatted summary for use in conversation context

        Args:
            pdf_data: PDF data potentially containing summaries

        Returns:
            Formatted summary string or None
        """
        if not pdf_data.get('summarization_complete'):
            return None

        filename = pdf_data.get('filename', 'Document')
        doc_summary = pdf_data.get('document_summary', '')

        if doc_summary:
            return f"Document Summary for '{filename}':\n{doc_summary}"

        return None

    def summarize_pdf_sync(self, pdf_data: Dict) -> Dict:
        """
        Synchronous version of PDF summarization (DEPRECATED - use async version instead)

        This method is kept for backward compatibility but should not be used.
        Use summarize_pdf_recursive for full document processing.

        Args:
            pdf_data: PDF data from NVINGEST containing pages

        Returns:
            Dictionary containing original data plus summaries
        """
        try:
            pages = pdf_data.get('pages', [])
            total_pages = len(pages)
            filename = pdf_data.get('filename', 'Unknown')

            logger.warning(
                f"Using deprecated sync summarization for {filename} ({total_pages} pages). Use async version for better results."
            )

            if total_pages == 0:
                return pdf_data

            # Process all pages (removed 20-page limit)
            pages_to_process = pages  # Process all pages instead of limiting to 20

            # Create a quick summary
            combined_text = "\n\n".join(
                [
                    f"Page {page.get('page', i+1)}:\n{page.get('text', '')[:1000]}"  # Limit text per page
                    for i, page in enumerate(pages_to_process)
                ]
            )

            summary_params = {
                "task_type": "summarize",
                "text": combined_text,
                "instructions": f"Create a concise summary of this document '{filename}'. Focus on the main topics and key information.",
            }

            # Import locally to avoid circular imports
            from tools.assistant import execute_assistant_with_dict

            summary_result = execute_assistant_with_dict(summary_params)

            # Add basic summary to PDF data
            enhanced_pdf_data = pdf_data.copy()
            enhanced_pdf_data['document_summary'] = summary_result.result
            enhanced_pdf_data['summarization_complete'] = True
            enhanced_pdf_data['summarization_type'] = 'quick'  # Mark as quick summary

            logger.info(f"Completed synchronous summarization for {filename}")
            return enhanced_pdf_data

        except Exception as e:
            logger.error(f"Error in synchronous summarization: {e}")
            return pdf_data

    def __del__(self):
        """Cleanup executor on deletion"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)
