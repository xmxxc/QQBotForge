const assert=require('node:assert/strict');
const {test}=require('node:test');
const {page}=require('./account_page.cjs');
test('current report fields explain every action and incomplete failure coverage',()=>{
 const p=page();
 p.ctx.deliveryReport({group:'group-a',resource:'r',report:{complete:false,failures_complete:false,reason:'roster_incomplete',roster_reason:'permission_denied',members:[{member:'member-a',actions:{invoke:{allowed:false,reason:'expired'},view:{allowed:false,reason:'denied'}}}]}});
 const summary=JSON.parse(p.nodes['delivery-summary'].textContent);
 assert.equal(summary['名单完整'],false); assert.equal(summary['失败名单完整'],false);
 assert.match(summary['名册限制'],/不能视为全部/); assert.equal(summary['名册原因'],'permission_denied');
 const text=p.nodes['delivery-report'].children[0].textContent;
 assert.match(text,/调用：授权已到期/); assert.match(text,/查看：被拒绝规则拦截/); assert.doesNotMatch(text,/undefined/);
 p.ctx.deliveryReport({report:{complete:true,failures_complete:true,members:[]}});
 assert.equal(JSON.parse(p.nodes['delivery-summary'].textContent)['名单完整'],true);
});
if(process.argv[2]) {
 const p=page();p.ctx.deliveryReport(JSON.parse(process.argv[2]));
 console.log(JSON.stringify({summary:p.nodes['delivery-summary'].textContent,rows:p.nodes['delivery-report'].children.map(n=>n.textContent)}));
}
