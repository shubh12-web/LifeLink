
async function jsonAction(url, options={}){
  const r=await fetch(url,{headers:{'Content-Type':'application/json'},...options});
  let data={}; try{data=await r.json()}catch(e){}
  if(!r.ok) throw new Error(data.error||'Request failed');
  return data;
}
async function invAction(id, action){
  try{const d=await jsonAction(`/api/invitations/${id}/${action}`,{method:'POST'}); alert('Invitation '+d.status+'.'); location.reload();}
  catch(e){alert(e.message)}
}
async function runMatching(id){
  try{const d=await jsonAction(`/api/matching/${id}`,{method:'POST'}); alert(`Matching complete. Radius: ${d.radius_km} km. New candidates: ${d.new_candidates}.`); location.reload();}
  catch(e){alert(e.message)}
}
async function setState(id,state){
  try{const d=await jsonAction(`/api/requests/${id}/state`,{method:'POST',body:JSON.stringify({state})}); alert('Request state: '+d.status); location.reload();}
  catch(e){alert(e.message)}
}
async function verifyHospital(id){
  try{await jsonAction(`/api/hospitals/${id}/verify`,{method:'POST'}); alert('Hospital verified.'); location.reload();}
  catch(e){alert(e.message)}
}
async function connectOrgan(id){
  try{await jsonAction(`/api/organ/${id}/connect`,{method:'POST'}); alert('Organ pledge connection saved.'); location.reload();}
  catch(e){alert(e.message)}
}
