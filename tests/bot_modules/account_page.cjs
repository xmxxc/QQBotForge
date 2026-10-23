// Execute the shipped page script; replace only DOM and fetch, export its real requests.
const fs=require('node:fs'), vm=require('node:vm');
function page(resource='account-id') {
 const html=fs.readFileSync(require('node:path').join(__dirname,'../../plugins/account_sharing/page.html'),'utf8');
 function element(){return {value:'',checked:false,selectedOptions:[],options:[],children:[],textContent:'',append(...xs){this.children.push(...xs)},replaceChildren(){this.children=[]},reset(){}}}
 const nodes=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],element()]));
 const requests=[];
 const ctx={document:{getElementById:id=>nodes[id],createElement:element,createTextNode:text=>({textContent:text})},URLSearchParams,confirm:()=>true,
 fetch:async(url,options)=>{requests.push({url,...options,body:JSON.parse(options.body||'null')});return {ok:true,json:async()=>({data:{summary:{success:1,failed:0}}})}}};
 vm.createContext(ctx); vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],ctx);
 for(const [id,value] of Object.entries({bot:'bot-a',group:'group-a',member:'member-a','identity-type':'group_member_openid','function-scope-kind':'function','function-scope-id':'account_sharing','resource-scope-kind':'resources','grant-mode':'snapshot','bulk-members':'member-a\nmissing-member'}))nodes[id].value=value;
 nodes.invoke.checked=true;nodes.confirmed.checked=true;nodes.resources.selectedOptions=[{value:resource}];
 return {nodes,requests,ctx};
}
module.exports={page};
if(require.main===module)(async()=>{const p=page(process.argv[2]);await p.nodes['access-actions'].children.find(b=>b.textContent==='授权').onclick();for(const id of ['bulk-grant','bulk-revoke','bulk-expiry'])await p.nodes[id].onclick(); console.log(JSON.stringify(p.requests));})();
