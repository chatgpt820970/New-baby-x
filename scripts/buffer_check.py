import json,os,sys,urllib.request
K=os.environ["BUFFER_API_KEY"];PID=sys.argv[1];ACT=sys.argv[2]
def q(query,v=None):
    r=urllib.request.Request("https://api.buffer.com",data=json.dumps({"query":query,"variables":v or {}}).encode(),headers={"Authorization":"Bearer "+K,"Content-Type":"application/json","User-Agent":"clips-poster"})
    return json.load(urllib.request.urlopen(r,timeout=60))
P="query($id:PostId!){post(input:{id:$id}){id status text dueAt sentAt createdAt externalLink error{message rawError}}}"
r=q(P,{"id":PID});print("POST",json.dumps(r))
post=(r.get("data") or {}).get("post") or {}
if ACT=="delete" and post and post.get("status")!="sent":
    print("DELETE",json.dumps(q("mutation($id:PostId!){deletePost(input:{id:$id}){__typename ... on MutationError{message}}}",{"id":PID})))
o=q("query{account{organizations{id}}}");org=o["data"]["account"]["organizations"][0]["id"]
L=q("query($o:OrganizationId!){posts(first:20,input:{organizationId:$o,sort:[{field:createdAt,direction:desc}]}){edges{node{id status createdAt dueAt sentAt externalLink text error{message}}}}}",{"o":org})
for e in (L.get("data") or {}).get("posts",{}).get("edges",[]) or []:
    n=e["node"];print("RECENT",n["id"],n["status"],n["createdAt"],n["sentAt"],n["externalLink"],(n["error"] or {}).get("message"),n["text"][:40])
if L.get("errors"):print("ERR",L["errors"])
