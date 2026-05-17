from bs4 import BeautifulSoup
import re
import json

def parse_retsinformation_html(html_file_path, doc_id="pensionsloven"):
    with open(html_file_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f, 'html.parser')
    
    chunks = []
    current_chunk = None
    global_chunk_idx = -1 
    
    paragraf_stks = {} 
    current_paragraf_id_clean = None
    
    #tracks if we have hit the cutoff point - everything after the first centererd paragraph is not typical legal section and should
    #not be treated as such. However, they have the same class identifiers as actual legal sections, so we must change handling
    #of document after first one is encoutered.
    has_hit_centreret_paragraf = False
    
    for element in soup.find_all('p'):
        raw_classes = element.get('class', [])
        if isinstance(raw_classes, str): 
            raw_classes = [raw_classes]
        classes_lower = [str(c).lower() for c in raw_classes]
        
        # Check for cutoff
        if 'centreretparagraf' in classes_lower:
            has_hit_centreret_paragraf = True
        
        #1. identify new paragraph (legal section) chunk 
        #(stk 1 is implicit)
        if not has_hit_centreret_paragraf and ('paragraph' in classes_lower or 'paragraf' in classes_lower):
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = None
            
            span = element.find('span', class_=re.compile('paragrafnr', re.IGNORECASE))
            if span:
                span_text = span.get_text()
                raw_id = span_text.replace('\xa0', '').replace(' ', '').strip('.')
            else:
                span_text = ""
                raw_id = "UNKNOWN"
            
            if not raw_id.startswith('§') and raw_id != "UNKNOWN":
                raw_id = '§' + raw_id
            
            current_paragraf_id_clean = re.sub(r'\s+', '', raw_id)
            chunk_id = f"{doc_id}_{current_paragraf_id_clean}_stk1"
            
            if current_paragraf_id_clean not in paragraf_stks:
                paragraf_stks[current_paragraf_id_clean] = []
            paragraf_stks[current_paragraf_id_clean].append(chunk_id)
            
            global_chunk_idx += 1
            current_chunk = {
                "chunk": element.get_text(separator=" ", strip=True) + "\n",
                "chunk_id": chunk_id,
                "chunk_idx": global_chunk_idx,
                "base_paragraf_nr": current_paragraf_id_clean,
                "span_text": span_text,
                "is_stk": True 
            }
            
        #2. append sub-elements or create new stk
        elif not has_hit_centreret_paragraf and 'stk2' in classes_lower:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = None
                
            span = element.find('span', class_=re.compile('stknr', re.IGNORECASE))
            if span:
                span_text = span.get_text()
                stk_raw = span_text.lower().replace('\xa0', '').replace(' ', '').replace('.', '')
            else:
                span_text = ""
                stk_raw = "stk_unknown"
                
            chunk_id = f"{doc_id}_{current_paragraf_id_clean}_{stk_raw}"
            
            if current_paragraf_id_clean not in paragraf_stks:
                paragraf_stks[current_paragraf_id_clean] = []
            paragraf_stks[current_paragraf_id_clean].append(chunk_id)
            
            global_chunk_idx += 1
            current_chunk = {
                "chunk": element.get_text(separator=" ", strip=True) + "\n",
                "chunk_id": chunk_id,
                "chunk_idx": global_chunk_idx,
                "base_paragraf_nr": current_paragraf_id_clean,
                "span_text": span_text,
                "is_stk": True 
            }
            
        elif not has_hit_centreret_paragraf and any(c in classes_lower for c in ['liste1', 'liste2', 'tekstgenerel']):
            if current_chunk and current_chunk.get("is_stk"):
                current_chunk['chunk'] += element.get_text(separator=" ", strip=True) + "\n"
            else:
                text = element.get_text(separator=" ", strip=True)
                if text:
                    global_chunk_idx += 1
                    chunks.append({
                        "chunk": text + "\n",
                        "chunk_id": f"{doc_id}_nonpar_{global_chunk_idx}",
                        "chunk_idx": global_chunk_idx,
                        "is_stk": False
                    })

        # 3.HANDLING OF EVERYTHING ELSE (including everything after first CentreretParagraf)
        else:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = None
            
            text = element.get_text(separator=" ", strip=True)
            if text: 
                global_chunk_idx += 1
                chunks.append({
                    "chunk": text + "\n",
                    "chunk_id": f"{doc_id}_nonpar_{global_chunk_idx}",
                    "chunk_idx": global_chunk_idx,
                    "is_stk": False
                })

    if current_chunk:
        chunks.append(current_chunk)

    # set of valid chunks (used to filter out hallucinated regex matches like §83i)
    all_valid_chunk_ids = {c["chunk_id"] for c in chunks}

    #4. second pass - resolving context
    query_candidate_count = 0
    jsonl_output = []
    
    for c in chunks:
        implicit_context = set()
        explicit_context = set()
        
        #Elements after CentreretParagraf are "is_stk": False, so this skips them safely
        if c.get("is_stk"):
            #A. implicit context
            base_p = c.get("base_paragraf_nr")
            if base_p and base_p in paragraf_stks:
                for sibling_id in paragraf_stks[base_p]:
                    if sibling_id != c["chunk_id"]:
                        implicit_context.add(sibling_id)
            
            #B. explicitcContext (Now passing base_p down to extract internal stk references)
            raw_explicit_refs = extract_references(
                c['chunk'], 
                c['chunk_id'], 
                c['span_text'], 
                paragraf_stks, 
                base_p,
                doc_id
            )
            
            #C. ensure the extracted reference actually exists in the document
            valid_explicit_refs = {ref for ref in raw_explicit_refs if ref in all_valid_chunk_ids}
            
            for ref in valid_explicit_refs:
                if ref != c["chunk_id"]:
                    explicit_context.add(ref)
            
            if len(explicit_context) > 0:
                query_candidate_count += 1
            
        final_dict = {
            "chunk": c["chunk"].strip(),
            "chunk_id": c["chunk_id"],
            "chunk_idx": c["chunk_idx"],
            "implicit_context_chunks": sorted(list(implicit_context)),
            "explicit_context_chunks": sorted(list(explicit_context))
        }
        jsonl_output.append(json.dumps(final_dict, ensure_ascii=False))
        
    return "\n".join(jsonl_output), query_candidate_count


def extract_references(text, current_chunk_id, span_text_to_ignore, paragraf_stks, current_base_paragraf, doc_id="serviceloven"):
    referenced_chunk_ids = set()
    
    if span_text_to_ignore:
        text_for_parsing = text.replace(span_text_to_ignore, "", 1)
    else:
        text_for_parsing = text

    def expand_paragraph_lists(t):
        item_pattern = r'\d+(?:\s*[a-z]\b)?(?:\s*-\s*\d+(?:\s*[a-z]\b)?)?'
        list_pattern = r'§§?\s*(' + item_pattern + r'(?:(?:\s*,\s*|\s+og\s+)' + item_pattern + r')*)'
        
        def replace_list(match):
            list_str = match.group(1)
            items = re.split(r'\s*,\s*|\s+og\s+', list_str)
            return ' og '.join(['§ ' + item.strip() for item in items if item.strip()])

        return re.sub(list_pattern, replace_list, t, flags=re.IGNORECASE)

    text_expanded = expand_paragraph_lists(text_for_parsing)
    text_clean = re.sub(r'§§?[^§\.]*?i lov\b', '', text_expanded, flags=re.IGNORECASE)

    # We use this copy to erase matched explicit references, so they don't trigger Rule 3
    text_for_internal_search = text_clean 

    #rule 1: HAndling ranges
    ranges = re.finditer(r'§§?\s*(\d+)\s*-\s*(\d+)', text_clean)
    for match in ranges:
        text_for_internal_search = text_for_internal_search.replace(match.group(0), '') # Mask out
        start, end = match.groups()
        for i in range(int(start), int(end) + 1):
            ref_base_nr = f"§{i}"
            if ref_base_nr in paragraf_stks:
                for stk_id in paragraf_stks[ref_base_nr]:
                    referenced_chunk_ids.add(stk_id)

    #Rule 2: extract individual paragraphs
    pattern = r'§\s*(\d+(?:\s*[a-z]\b)?)(?:,\s*(stk\.\s*\d+(?!\.\s*(?:pkt|punktum))(?:\s*(?:,|og|-)\s*(?:stk\.\s*)?\d+(?!\.\s*(?:pkt|punktum)))*))?'
    single_refs = re.finditer(pattern, text_clean, flags=re.IGNORECASE)
    
    for match in single_refs:
        text_for_internal_search = text_for_internal_search.replace(match.group(0), '') # Mask out
        raw_num = match.group(1)
        stk_str = match.group(2)
        
        clean_num = re.sub(r'\s+', '', raw_num) 
        ref_base_nr = f"§{clean_num}"
        
        if stk_str:
            nums = set()
            stk_ranges = re.findall(r'(\d+)\s*-\s*(\d+)', stk_str)
            for start, end in stk_ranges:
                for i in range(int(start), int(end) + 1):
                    nums.add(str(i))
                    
            stk_str_no_ranges = re.sub(r'\d+\s*-\s*\d+', '', stk_str)
            stk_singles = re.findall(r'\d+', stk_str_no_ranges)
            for s in stk_singles:
                nums.add(s)
                
            for n in nums:
                ref_chunk_id = f"{doc_id}_{ref_base_nr}_stk{n}"
                referenced_chunk_ids.add(ref_chunk_id)
        else:
            if ref_base_nr in paragraf_stks:
                for stk_id in paragraf_stks[ref_base_nr]:
                    referenced_chunk_ids.add(stk_id)
            else:
                referenced_chunk_ids.add(f"{doc_id}_{ref_base_nr}_stk1")

    #rule 3: Internal Stk References (Using the masked text_for_internal_search)
    #Extracts "stk. 2", "stk. 1 og 3", etc., that were NOT attached to a paragraph symbol
    if current_base_paragraf:
        stk_only_pattern = r'stk\.\s*\d+(?!\.\s*(?:pkt|punktum))(?:\s*(?:,|og|-)\s*(?:stk\.\s*)?\d+(?!\.\s*(?:pkt|punktum)))*'
        internal_refs = re.finditer(stk_only_pattern, text_for_internal_search, flags=re.IGNORECASE)
        for match in internal_refs:
            stk_str = match.group(0)
            nums = set()
            
            stk_ranges = re.findall(r'(\d+)\s*-\s*(\d+)', stk_str)
            for start, end in stk_ranges:
                for i in range(int(start), int(end) + 1):
                    nums.add(str(i))
                    
            stk_str_no_ranges = re.sub(r'\d+\s*-\s*\d+', '', stk_str)
            stk_singles = re.findall(r'\d+', stk_str_no_ranges)
            for s in stk_singles:
                nums.add(s)
                
            for n in nums:
                ref_chunk_id = f"{doc_id}_{current_base_paragraf}_stk{n}"
                referenced_chunk_ids.add(ref_chunk_id)

    #remove self-reference if it snuck in
    if current_chunk_id in referenced_chunk_ids:
        referenced_chunk_ids.remove(current_chunk_id)

    return referenced_chunk_ids

if __name__ == "__main__":
    jsonl_data, total_candidates = parse_retsinformation_html('Pensionsloven.html') 
    print(f"\n--- PARSING COMPLETE ---")
    print(f"Found {total_candidates} query candidates in this document.")
     
    with open('pensionsloven.jsonl', 'w', encoding='utf-8') as out_file:
         out_file.write(jsonl_data)