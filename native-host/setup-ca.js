'use strict';
const fs=require('fs'),path=require('path'),os=require('os'),forge=require('node-forge');
const CD=path.join(os.homedir(),'.proxy-bridge-ca'),CC=path.join(CD,'ca-cert.pem'),CK=path.join(CD,'ca-key.pem');
function gCA(){
  if(!fs.existsSync(CC)||!fs.existsSync(CK)){
    if(!fs.existsSync(CD))fs.mkdirSync(CD,{recursive:true});
    const k=forge.pki.rsa.generateKeyPair(2048),c=forge.pki.createCertificate();
    c.publicKey=k.publicKey;c.serialNumber='01';c.validity.notBefore=new Date();c.validity.notAfter=new Date();
    c.validity.notAfter.setFullYear(c.validity.notAfter.getFullYear()+10);
    c.setSubject([{name:'commonName',value:'Proxy Bridge Local CA'}]);c.setIssuer(c.subject.attributes);
    c.setExtensions([{name:'basicConstraints',cA:true},{name:'keyUsage',keyCertSign:true,cRLSign:true},{name:'subjectKeyIdentifier'}]);
    c.sign(k.privateKey,forge.md.sha256.create());
    fs.writeFileSync(CC,forge.pki.certificateToPem(c),'utf8');fs.writeFileSync(CK,forge.pki.privateKeyToPem(k.privateKey),'utf8');fs.chmodSync(CK,0o600);
  }
  return {cert:forge.pki.certificateFromPem(fs.readFileSync(CC,'utf8')),key:forge.pki.privateKeyFromPem(fs.readFileSync(CK,'utf8'))};
}
let cc=null;function rCA(){return cc||(cc=gCA());}
function gCH(h){
  const ca=rCA(),k=forge.pki.rsa.generateKeyPair(2048),c=forge.pki.createCertificate();
  c.publicKey=k.publicKey;c.serialNumber='01';c.validity.notBefore=new Date(Date.now()-86400000);c.validity.notAfter=new Date(Date.now()+365*86400000);
  c.setSubject([{name:'commonName',value:h}]);c.setIssuer(ca.cert.subject.attributes);
  c.setExtensions([{name:'basicConstraints',cA:false},{name:'keyUsage',digitalSignature:true,keyEncipherment:true},{name:'extKeyUsage',serverAuth:true},{name:'subjectAltName',altNames:[{type:2,value:h}]},{name:'subjectKeyIdentifier'}]);
  c.sign(ca.key,forge.md.sha256.create());
  return {cert:forge.pki.certificateToPem(c),key:forge.pki.privateKeyToPem(k.privateKey)};
}
module.exports={getCertForHost:gCH,getCachedCertForHost:gCH};
