from bs4 import BeautifulSoup
import re
import json

def parse_retsinformation_html(html_file_path, doc_id="serviceloven"):
    with open(html_file_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f, 'html.parser')
    
    chunks = []
    current_chunk = None
    global_chunk_idx = -1 
    
    for element in soup.find_all('p'):
        raw_classes = element.get('class', [])
        if isinstance(raw_classes, str): 
            raw_classes = [raw_classes]
        classes_lower = [str(c).lower() for c in raw_classes]
        
        #1. identification of legal section (paragraf) chunk
        if 'paragraph' in classes_lower or 'paragraf' in classes_lower:
            if current_chunk:
                current_chunk['context_chunks'] = extract_references(
                    current_chunk['chunk'], 
                    current_chunk['chunk_id'], 
                    current_chunk['span_text'],
                    doc_id
                )
                chunks.append(current_chunk)
                current_chunk = None
            
            span = element.find('span', class_=re.compile('paragrafnr', re.IGNORECASE))
            if span:
                span_text = span.get_text()
                raw_id = span_text.replace('\xa0', '').replace(' ', '').strip('.')
            else:
                span_text = ""
                raw_id = "UNKNOWN"
            
            global_chunk_idx += 1
            current_chunk = {
                "chunk": element.get_text(separator=" ", strip=True) + "\n",
                "chunk_id": f"{doc_id}_{raw_id}",
                "chunk_idx": global_chunk_idx,
                "base_paragraf_nr": raw_id,
                "span_text": span_text,
                "context_chunks": [],
                "is_paragraph": True 
            }
            
        #2. append subelements to current paragraf
        elif any(c in classes_lower for c in ['stk2', 'liste1', 'liste2', 'tekstgenerel']):
            if current_chunk and current_chunk.get("is_paragraph"):
                current_chunk['chunk'] += element.get_text(separator=" ", strip=True) + "\n"
            else:
                text = element.get_text(separator=" ", strip=True)
                if text:
                    global_chunk_idx += 1
                    chunks.append({
                        "chunk": text + "\n",
                        "chunk_id": f"{doc_id}_nonpar_{global_chunk_idx}",
                        "chunk_idx": global_chunk_idx,
                        "context_chunks": [],
                        "is_paragraph": False
                    })

        # 3. handling of everything else
        else:
            if current_chunk:
                current_chunk['context_chunks'] = extract_references(
                    current_chunk['chunk'], 
                    current_chunk['chunk_id'],
                    current_chunk['span_text'],
                    doc_id
                )
                chunks.append(current_chunk)
                current_chunk = None
            
            text = element.get_text(separator=" ", strip=True)
            if text: 
                global_chunk_idx += 1
                chunks.append({
                    "chunk": text + "\n",
                    "chunk_id": f"{doc_id}_nonpar_{global_chunk_idx}",
                    "chunk_idx": global_chunk_idx,
                    "context_chunks": [],
                    "is_paragraph": False
                })

    # save the final chunk if the document ends on a paragraph
    if current_chunk:
        current_chunk['context_chunks'] = extract_references(
            current_chunk['chunk'], 
            current_chunk['chunk_id'],
            current_chunk['span_text'],
            doc_id
        )
        chunks.append(current_chunk)

    # 4. FORMAT TO JSONL & CLEAN UP TEMPORARY KEYS
    query_candidate_count = 0
    jsonl_output = []
    
    for c in chunks:
        if c.get("is_paragraph") and len(c["context_chunks"]) > 0:
            query_candidate_count += 1
            
        final_dict = {
            "chunk": c["chunk"].strip(),
            "chunk_id": c["chunk_id"],
            "chunk_idx": c["chunk_idx"],
            "context_chunks": c["context_chunks"]
        }
        jsonl_output.append(json.dumps(final_dict, ensure_ascii=False))
        
    return "\n".join(jsonl_output), query_candidate_count


def extract_references(text, current_chunk_id, span_text_to_ignore, doc_id="serviceloven"):
    referenced_chunk_ids = set()
    
    if span_text_to_ignore:
        text_for_parsing = text.replace(span_text_to_ignore, "", 1)
    else:
        text_for_parsing = text
    
    text_clean = re.sub(r'(§§?\s*[\d\s\-,og]+.*?i lov)', '', text_for_parsing, flags=re.IGNORECASE)

    #1: extract and normalize ranges 
    ranges = re.findall(r'§§\s*(\d+)\s*-\s*(\d+)', text_clean)
    for start, end in ranges:
        for i in range(int(start), int(end) + 1):
            ref_chunk_id = f"{doc_id}_§{i}"
            if ref_chunk_id != current_chunk_id: 
                referenced_chunk_ids.add(ref_chunk_id)

    #2: extract individual paragraphs and mixed lists
    # Added \b (word boundary) to ensure the letter is a standalone identifier, not the start of a word.
    single_refs = re.findall(r'§\s*(\d+(?:\s*[a-z]\b)?)', text_clean, flags=re.IGNORECASE)
    for num in single_refs:
        clean_num = re.sub(r'\s+', '', num) 
        ref_chunk_id = f"{doc_id}_§{clean_num}"
        
        if ref_chunk_id != current_chunk_id:
            referenced_chunk_ids.add(ref_chunk_id)

    return sorted(list(referenced_chunk_ids))

jsonl_data, total_candidates = parse_retsinformation_html('Serviceloven.html')
print(f"\n--- PARSING COMPLETE ---")
print(f"Found {total_candidates} query candidates in this document.")
 
with open('outputcurrent.jsonl', 'w', encoding='utf-8') as out_file:
     out_file.write(jsonl_data)