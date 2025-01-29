import os
import json
import html
import argparse
import boto3
import tempfile
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
from jinja2 import Template
import smart_open
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
import markdown2

from olmocr.s3_utils import get_s3_bytes, parse_s3_path
from olmocr.data.renderpdf import render_pdf_to_base64webp

def read_jsonl(path):
    with smart_open.smart_open(path, 'r', encoding='utf-8') as f:
        for line in f:
            yield line.strip()

def generate_presigned_url(s3_client, bucket_name, key_name):
    try:
        response = s3_client.generate_presigned_url('get_object',
                                                    Params={'Bucket': bucket_name, 'Key': key_name},
                                                    ExpiresIn=3600 * 24 * 7 - 100)  # Link expires in 1 week
        return response
    except (NoCredentialsError, PartialCredentialsError):
        print("Error: AWS credentials not found or incomplete.")
        return None

def process_document(data, s3_client, template, output_dir):
    id_ = data.get('id')
    text = data.get('text', '')
    attributes = data.get('attributes', {})
    pdf_page_numbers = attributes.get('pdf_page_numbers', [])
    metadata = data.get('metadata', {})
    source_file = metadata.get('Source-File')

    # Generate base64 image of the corresponding PDF page
    local_pdf = tempfile.NamedTemporaryFile("wb+", suffix=".pdf")
    local_pdf.write(get_s3_bytes(s3_client, source_file))
    local_pdf.flush()

    pages = []
    for span in pdf_page_numbers:
        start_index, end_index, page_num = span
        page_text = text[start_index:end_index]
        
        # Detect and convert Markdown to HTML
        page_text = html.escape(page_text, quote=True).replace('&lt;br&gt;', '<br>')
        page_text = markdown2.markdown(page_text, extras=["tables"])

        base64_image = render_pdf_to_base64webp(local_pdf.name, page_num)

        pages.append({'page_num': page_num, 'text': page_text, 'image': base64_image})

    local_pdf.close()

    # Generate pre-signed URL if source_file is an S3 path
    s3_link = None
    if source_file and source_file.startswith('s3://'):
        bucket_name, key_name = parse_s3_path(source_file)
        s3_link = generate_presigned_url(s3_client, bucket_name, key_name)

    # Render the HTML using the Jinja template
    html_content = template.render(id=id_, pages=pages, s3_link=s3_link)

    # Write the HTML content to a file
    filename = f'{source_file.replace("s3://", "").replace("/", "_").replace(".", "_")}.html'
    filepath = os.path.join(output_dir, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_content)

def main(jsonl_path, output_dir, template_path):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Load the Jinja template
    with open(os.path.join(os.path.dirname(__file__), template_path), 'r', encoding='utf-8') as template_file:
        template_content = template_file.read()
        template = Template(template_content)

    # Initialize S3 client for generating presigned URLs
    workspace_session = boto3.Session(profile_name="s2")
    s3_client = workspace_session.client("s3")

    # Create ThreadPoolExecutor
    with ThreadPoolExecutor() as executor:
        futures = []
        for line in read_jsonl(jsonl_path):
            if not line:
                continue
            data = json.loads(line)
            future = executor.submit(process_document, data, s3_client, template, output_dir)
            futures.append(future)

        for future in tqdm(as_completed(futures), total=len(futures)):
            try:
                future.result()
            except Exception as e:
                print(f"An error occurred: {e}")
                raise

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate HTML pages from a JSONL file with pre-signed S3 links.')
    parser.add_argument('jsonl_path', help='Path to the JSONL file (local or s3://)')
    parser.add_argument('--output_dir', default='dolma_previews', help='Directory to save HTML files')
    parser.add_argument('--template_path', default='dolmaviewer_template.html', help='Path to the Jinja2 template file')
    args = parser.parse_args()

    main(args.jsonl_path, args.output_dir, args.template_path)
