import json
import re
from pathlib import Path

from bs4 import BeautifulSoup
from bs4.element import Tag
from loguru import logger
import typer

app = typer.Typer()

@app.command()
def chunk_document(
    input_file: Path = typer.Argument(..., help="Path to the downloaded HTML document"),
    output_path: Path = typer.Argument(..., help="Path for the output .jsonl file"),
):
    logger.info(f"Chunking document: {input_file}")

    if not input_file.is_file() or not input_file.name.endswith('.html'):
        logger.error(f"Invalid file: {input_file}")
        return

    doc_name = input_file.stem
    doc_chunks = []
    
    with open(input_file, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f, 'html.parser')
        

        for header in soup.find_all(['h1', 'h2', 'h3', 'span']):
            if "treść obwieszczenia" in header.get_text(strip=True).lower():
                container = header.find_parent('div', class_='part') or header.find_parent('section')
                if container:
                    container.decompose()
                    logger.info("Detected and removed 'Treść obwieszczenia' section.")
                break 


        xtext_nodes = soup.select('[data-template="xText"]')
        
        node_to_chunkid = {}
        html_id_to_chunkids = {}
        
        seq = 1
        for node in xtext_nodes:
            cid = f"{doc_name}_{seq}"
            node_to_chunkid[node] = cid
            
            parent_units = node.find_parents('div', class_='unit')
            for pu in parent_units:
                uid = pu.get('id')
                if uid:
                    if uid not in html_id_to_chunkids:
                        html_id_to_chunkids[uid] = []
                    html_id_to_chunkids[uid].append(cid)
            seq += 1

        def get_direct_xtext_chunk_id(unit_id):
            unit = soup.find(id=unit_id)
            if not unit: return None
            inner = unit.find('div', class_='unit-inner', recursive=False)
            if not inner: return None
            xtext = inner.find('div', attrs={'data-template': 'xText'}, recursive=False)
            if not xtext: return None
            return node_to_chunkid.get(xtext)


        def expand_range(match_str):
            if not match_str: return []
            match_str = match_str.replace(" ", "")
            if '-' not in match_str:
                return [match_str]
            
            try:
                start_str, end_str = match_str.split('-', 1)
                match_start = re.match(r'^(\d+)([a-z]*)$', start_str)
                match_end = re.match(r'^(\d+)([a-z]*)$', end_str)

                if match_start and match_end:
                    start_num, start_let = match_start.groups()
                    end_num, end_let = match_end.groups()

                    if start_num == end_num and start_let and end_let:
                        return [f"{start_num}{chr(c)}" for c in range(ord(start_let), ord(end_let) + 1)]
                    elif not start_let and not end_let:
                        return [str(i) for i in range(int(start_num), int(end_num) + 1)]
            except Exception:
                pass # Fallback if parsing fails
            return [match_str]


        def parse_list(match_str):
            if not match_str: return []
            # Split by comma, 'i', or 'oraz'
            parts = re.split(r',|\bi\b|\boraz\b', match_str)
            results = []
            for p in parts:
                p = p.strip()
                if p:
                    results.extend(expand_range(p))
            return results

        ref_pattern = re.compile(
            r'(?:art\.\s*(?P<art>\d+[a-z]*(?:\s*(?:-|,|i|oraz)\s*\d+[a-z]*)*)\s*)?'
            r'(?:ust\.\s*(?P<pass>\d+[a-z]*(?:\s*(?:-|,|i|oraz)\s*\d+[a-z]*)*)\s*)?'
            r'(?:pkt\s*(?P<pint>\d+[a-z]*(?:\s*(?:-|,|i|oraz)\s*\d+[a-z]*)*))?',
            re.IGNORECASE
        )

        external_keywords = ['ustawy', 'ustawie', 'kodeksu', 'kodeksie', 'prawa', 'prawie']


        body = soup.body
        first_h1 = body.find('h1') if body else None
        if first_h1:
            doc_chunks.append({
                'chunk_id': f"{doc_name}_0",
                'chunk': ' '.join(first_h1.get_text(separator=' ').split()),
                'implicit_context_chunks': [],
                'explicit_context_chunks': []
            })

        seq = 1
        chunks_with_implicit = 0
        chunks_with_explicit = 0

        for node in xtext_nodes:
            cid = f"{doc_name}_{seq}"
            
            chunk_text = node.get_text(separator=' ')
            chunk_text = ' '.join(chunk_text.split())
            
            parent = node.parent
            h3_text = None
            for sibling in parent.previous_siblings:
                if getattr(sibling, 'name', None) == 'h3':
                    h3_text = ' '.join(sibling.get_text(separator=' ').split())
                    break
                    
            if h3_text:
                full_chunk = h3_text + " " + chunk_text
                if h3_text == '1.':
                    grandgrandparent = parent.parent.parent
                    if grandgrandparent:
                        par_h3_text = None
                        for sibling in grandgrandparent.previous_siblings:
                            if getattr(sibling, 'name', None) == 'h3':
                                par_h3_text = ' '.join(sibling.get_text(separator=' ').split())
                                break
                        if par_h3_text:
                            full_chunk = par_h3_text + ' ' + full_chunk
            else:
                full_chunk = chunk_text

            parent_unit = node.find_parent('div', class_='unit')
            current_id = parent_unit.get('id') if parent_unit else ""
            
            target_html_ids = set()
            has_explicit_refs = False

            chunk_data = {
                'chunk_id': cid,
                'chunk': full_chunk,
                'implicit_context_chunks': [], 
                'explicit_context_chunks': []  
            }

            if current_id:
                implicit_chunk_ids = set()
                parts = current_id.split('-')
                ancestor_id = ""
                for i in range(len(parts) - 1): 
                    ancestor_id = parts[0] if i == 0 else f"{ancestor_id}-{parts[i]}"
                    direct_cid = get_direct_xtext_chunk_id(ancestor_id)
                    if direct_cid:
                        implicit_chunk_ids.add(direct_cid)

                implicit_chunk_ids.discard(cid)
                chunk_data['implicit_context_chunks'] = sorted(list(implicit_chunk_ids))


                curr_art_match = re.search(r'arti_(\d+[a-z]*)', current_id)
                curr_pass_match = re.search(r'pass_(\d+)', current_id)
                curr_art = curr_art_match.group(1) if curr_art_match else None
                curr_pass = curr_pass_match.group(1) if curr_pass_match else None

                for match in ref_pattern.finditer(chunk_text):
                    if not match.group(0).strip(): continue
                    
                    pre_text = chunk_text[max(0, match.start()-60):match.start()]
                    post_text = chunk_text[match.end():match.end()+120]
                    context_window = (pre_text + " " + post_text).lower()

                    if any(kw in context_window for kw in external_keywords):
                        continue
                        
                    ref_arts = parse_list(match.group('art'))
                    ref_passes = parse_list(match.group('pass'))
                    ref_pints = parse_list(match.group('pint'))
                    
                    if not (ref_arts or ref_passes or ref_pints): continue
                    
                    ref_arts = ref_arts or [None]
                    ref_passes = ref_passes or [None]
                    ref_pints = ref_pints or [None]

                    for p_art in ref_arts:
                        for p_pass in ref_passes:
                            for p_pint in ref_pints:
                                target_parts = []
                                
                                resolved_art = p_art or curr_art
                                if resolved_art:
                                    target_parts.append(f"arti_{resolved_art}")
                                    
                                resolved_pass = p_pass
                                if not resolved_pass and not p_art and curr_pass and p_pint:
                                    resolved_pass = curr_pass
                                    
                                if resolved_pass and target_parts:
                                    target_parts.append(f"pass_{resolved_pass}")
                                    
                                if p_pint and target_parts:
                                    target_parts.append(f"pint_{p_pint}")
                                    
                                target_id = "-".join(target_parts)
                                
                                if target_id:
                                    target_id_parts = target_id.split('-')
                                    
                                    if target_id_parts[0].startswith(('pass_', 'pint_')) and not curr_art:
                                        continue 

                                    matched_any = False
                                    for hid in html_id_to_chunkids.keys():
                                        hid_parts = hid.split('-')
                                        try:
                                            idx = hid_parts.index(target_id_parts[0])
                                            if hid_parts[idx:idx+len(target_id_parts)] == target_id_parts:
                                                target_html_ids.add(hid)
                                                matched_any = True
                                        except ValueError:
                                            continue
                                            
                                    if matched_any:
                                        has_explicit_refs = True

            # If explicit references were validated, attach them
            if has_explicit_refs:
                explicit_chunk_ids = set()
                for tid in target_html_ids:
                    explicit_chunk_ids.update(html_id_to_chunkids[tid])
                explicit_chunk_ids.discard(cid)
                chunk_data['explicit_context_chunks'] = sorted(list(explicit_chunk_ids))

            # Update counters
            if chunk_data['implicit_context_chunks']:
                chunks_with_implicit += 1
            if chunk_data['explicit_context_chunks']:
                chunks_with_explicit += 1

            doc_chunks.append(chunk_data)
            seq += 1

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as out_f:
        for chunk in doc_chunks:
            out_f.write(json.dumps(chunk, ensure_ascii=False) + '\n')

    logger.success(f"Chunking complete. {len(doc_chunks)} total chunks written to {output_path}")
    logger.info(f"Chunks containing implicit context: {chunks_with_implicit}")
    logger.info(f"Chunks containing explicit context: {chunks_with_explicit}")

if __name__ == "__main__":
    app()